"use strict";

// Shared Fetch API helper: JSON requests carry Flask-WTF's CSRF token.
window.agriLinkFetch = function (url, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = document.querySelector('meta[name="csrf-token"]')?.content;
  if (token && !["GET", "HEAD", "OPTIONS", "TRACE"].includes((options.method || "GET").toUpperCase())) {
    headers.set("X-CSRFToken", token);
  }
  return fetch(url, { ...options, headers, credentials: "same-origin" });
};

document.addEventListener("DOMContentLoaded", () => {
  const root = document.querySelector("[data-chat]");
  if (!root) return;
  const history = root.querySelector("[data-chat-history]");
  const form = root.querySelector("[data-message-form]");
  const input = form?.querySelector("textarea[name='body']");
  const status = root.querySelector("[data-chat-status]");
  const conversationId = Number(root.dataset.conversationId);
  const csrf = document.querySelector("meta[name='csrf-token']")?.content;
  const appendMessage = (message) => {
    if (history.querySelector(`[data-message-id='${message.id}']`)) return;
    history.querySelector("[data-empty-chat]")?.remove();
    const article = document.createElement("article");
    article.className = "chat-message";
    article.dataset.messageId = message.id;
    const body = document.createElement("p");
    body.className = "mb-0";
    body.textContent = message.body;
    const meta = document.createElement("div");
    meta.className = "small text-muted";
    meta.textContent = "Message sent";
    article.append(meta, body);
    history.append(article);
    history.scrollTop = history.scrollHeight;
  };
  if (window.io) {
    const socket = window.io();
    socket.on("connect", () => {
      socket.emit("conversation:join", { conversation_id: conversationId });
      socket.emit("conversation:read", { conversation_id: conversationId, csrf_token: csrf });
      if (status) status.textContent = "Live messages connected.";
    });
    socket.on("message:new", appendMessage);
    socket.on("error", (payload) => { if (status) status.textContent = payload?.error || "Message could not be sent."; });
    form?.addEventListener("submit", (event) => {
      if (!socket.connected || !input || !input.value.trim()) return;
      event.preventDefault();
      socket.emit("message:send", { conversation_id: conversationId, body: input.value, csrf_token: csrf });
      input.value = "";
    });
  } else if (csrf) {
    window.agriLinkFetch(`/api/conversations/${conversationId}/read`, { method: "POST" }).catch(() => {});
  }
});

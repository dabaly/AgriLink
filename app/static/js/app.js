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

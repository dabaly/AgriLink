# AgriLink

AgriLink is a mobile-responsive agricultural marketplace connecting farmers and buyers. The Flask application includes phone-verified accounts and a farmer marketplace for browsing and publishing listings.

## Requirements

- Python 3.14 or newer
- Git

## Development setup

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
flask --app wsgi.py run
```

The default development database is SQLite in `instance/agri_link.sqlite3`. SQLite foreign keys, WAL journaling, and a busy timeout are configured by the application. The `instance/` directory is created at startup and is ignored by Git.

## Authentication and phone verification

Registration accepts a phone number and password. Phone numbers are normalized to E.164 (national-format input is interpreted as Kenya by default). New accounts verify the phone using a six-digit code, then choose Farmer or Buyer once. A correct password on an unverified account starts a restricted verification flow; it does not establish a logged-in session. Normal login requires a verified phone and selected role. Passwords are hashed, and OTP challenges store only a challenge-specific HMAC. OTPs expire after five minutes, allow at most five attempts, and have a 60-second resend cooldown and five-send-per-hour phone limit.

Development and tests use `OTP_PROVIDER=mock` by default. The development provider writes codes to `instance/dev_otp_outbox.log`; this file is ignored by Git and must only be used in a local development environment. Tests capture messages in the mock provider in memory. Mock OTP delivery is rejected when running with the production configuration. Set `OTP_PEPPER` to a unique random value for production. The optional `OTP_PROVIDER=africastalking` adapter reads `AT_USERNAME`, `AT_API_KEY`, and `AT_SENDER_ID`; it has not been live-tested without provider credentials.

Apply the database schema before running the application:

```sh
flask --app wsgi.py db upgrade
```

## Marketplace and listings

Run `flask --app wsgi.py seed` after upgrading the database to create the platform-controlled Crops, Livestock, Inputs, and Equipment categories. The seed command is safe to rerun. It does not create demo accounts or sample listings.

Anonymous visitors can browse `/listings`, search listing titles and descriptions, and filter by category, county, grade, unit, and KES price range. Results sort by newest, oldest, or price and are paginated at 12 per page (maximum 24). `/api/listings` uses the same filtering and visibility rules and returns an explicit public JSON shape. `/listings/<id>` shows listing details; `/sellers/<user_id>` shows a farmer's public summary and current public listings.

Verified farmers can create listings at `/listings/new` and manage their own at `/my/listings`. A listing stores whole-number quantity and `price_minor` (KES cents; for example KES 150.00 is 15000). Listings start as `AVAILABLE` and `APPROVED`; normal public pages show only approved, available listings with positive quantity and an active, verified farmer. `UNAVAILABLE`, `SOLD`, and moderation-removed listings are excluded from public results. Moderation removal and seller status remain separate concepts.

Farmers may upload up to six JPEG, PNG, or WebP photos per listing. The request limit is 12 MB and each photo is limited to 5 MB. Pillow verifies and decodes the image, applies EXIF orientation, rejects inputs above 40 megapixels, resizes the full image to at most 1600 pixels, and writes a 400-pixel thumbnail. Both outputs are newly encoded JPEGs without source EXIF metadata. Generated UUID filenames are stored under `instance/uploads/listing_images/`, outside the static tree. Application image routes only serve an image belonging to a currently public listing or an authenticated listing owner; files are not served as an open directory.

Public responses do not include seller phone or email. Coordinates are optional and rounded to two decimal places in JSON, approximately one kilometre. Listing descriptions remain plain text and use normal Jinja escaping. The current profile model has no public display-name field, so seller pages use a generic farmer label alongside existing rating/order summary fields.

The schema adds `categories`, `listings`, and `listing_images`. Composite indexes support public listing filters and ordering, and a partial unique index enforces at most one primary photo per listing on SQLite. Listings do not have a seller hard-delete operation; farmers can mark them unavailable or sold, while moderation removal remains a distinct platform state. This preserves listing records for later marketplace history. Marketplace image deletion commits the database change before unlinking the generated full-size and thumbnail files; if an OS-level unlink fails, the database record is already gone and the orphan file is not addressable through application routes.

## Conversations, offers, and confirmed orders

Verified buyers can open a conversation from a public listing. Buyers and the listing's farmer can use `/conversations` and `/conversations/<id>` to read persistent messages and offer history. Messages can be sent through the CSRF-protected HTML/HTTP routes or authenticated Socket.IO events; Socket.IO clients join user rooms on connect and conversation rooms only after participant authorization. The HTTP route remains available if realtime transport is unavailable. Messages are plain text, immutable after sending, and limited to 2,000 characters; a shared per-user rate limit applies to HTTP and Socket.IO sends.

Either participant can make or counter one pending offer per conversation. Quantity and KES-per-unit price are validated on the server, totals use integer minor units, and offers expire after 48 hours. Expiry is also lazy-applied when offers are viewed or acted on. Run `flask --app wsgi.py jobs expire-offers` to expire stale pending offers in maintenance; `jobs run-maintenance` runs the same task.

Accepting another participant's unexpired offer atomically changes the offer to accepted, decrements available listing quantity, creates one confirmed order with a single immutable title/quantity/unit/price snapshot, and records its initial status history. The update only succeeds if the listing remains publicly purchasable and has sufficient stock. If the accepted offer consumes the remaining quantity, the listing becomes sold. This is the Batch 4 stock claim boundary; it does not implement reservation, checkout, payment, delivery, or subsequent order workflows.

The schema adds `conversations`, `messages`, `offers`, `orders`, `order_items`, and `order_status_history`. Conversations are unique per listing and buyer; messages and offers are retained as history. A partial unique index permits only one pending offer per conversation, and each confirmed order references its accepted offer uniquely. No phone numbers, email addresses, private profile fields, or filesystem details are included in chat payloads.

## Orders, inventory, and fulfillment

Buyers can view their orders at `/my/orders`; farmers can manage sales at `/my/sales`. Both roles can open only orders where they are the recorded buyer or seller. Order pages show the accepted-offer snapshot, total in integer KES minor units, private order history, and available actions. Every successful order state change and the initial `CONFIRMED` state creates a status history row.

An accepted offer atomically reduces `Listing.quantity`; that field continues to mean stock available for new offers. The `OrderItem` snapshot holds the quantity reserved by the order, so fulfillment never decrements listing stock a second time. Cancelling a `CONFIRMED` order before handoff atomically releases its reserved quantity. A `left_seller_at` timestamp records when delivery enters transit or a pickup is handed over; after that point a normal cancellation is rejected and no stock is restored.

The buyer chooses `PICKUP` or `DELIVERY` while an order is `CONFIRMED`. Pickup needs no destination address. Delivery requires a Kenyan county, area, directions, and recipient name and phone. These details are visible only to that order's buyer and seller. Once fulfillment starts, the method is locked. The seller can progress paid orders through preparation and the method-specific path: pickup goes through `READY_FOR_PICKUP`; delivery goes through `IN_TRANSIT`; both reach `DELIVERED` before buyer completion.

The lifecycle supports `PENDING`, `CONFIRMED`, `PAID`, `PROCESSING`, `READY_FOR_PICKUP`, `IN_TRANSIT`, `DELIVERED`, `COMPLETED`, `CANCELLED`, and `DISPUTED`. OrderService owns all transitions. This release has no PaymentService or payment provider, so orders remain `CONFIRMED` until a future trusted payment integration records payment; neither buyer nor seller can mark an order paid, and fulfillment cannot proceed before then. No payment success, charge, or refund is simulated. Delivered orders can be completed by the buyer or automatically after 72 hours using `flask --app wsgi.py jobs complete-orders`; `jobs run-maintenance` also runs that completion task with offer expiry.

The Batch 5 migration adds delivery instructions and physical handoff/completion timestamps, expands the order state constraints, and makes system-generated status history entries possible with a null actor. No payment provider, refund workflow, disputes UI, or background worker is included.

Run authentication, marketplace, chat, and offer tests with `pytest`. No real SMS provider is needed for local development or testing.

## Commands

```sh
flask --app wsgi.py --help
flask --app wsgi.py routes
flask --app wsgi.py db upgrade
flask --app wsgi.py create-admin
flask --app wsgi.py seed
flask --app wsgi.py jobs run-maintenance
flask --app wsgi.py jobs expire-offers
```

`flask db upgrade` applies the checked-in authentication, marketplace, conversation, offer, and order-foundation migrations. Use `flask --app wsgi.py db migrate -m "describe change"` to generate later schema revisions. The `create-admin` command prompts for a phone number and password in the terminal; administrator accounts cannot be created through public forms.

## Checks

```sh
pytest
ruff check .
bandit -r app
```

For deployment, set `AGRI_LINK_CONFIG=production` and provide a unique secret key and production database URL through the environment. HTTPS is required for secure production session cookies.

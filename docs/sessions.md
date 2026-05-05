# Trips & Multi-User Sessions

The app supports shared trips. One person starts a trip and gets an
invite code; anyone with the app can join as crew and see the same plan
in real time. No accounts, no signup.

---

## Concepts

- **Trip** — a shared session. Owns one ship, one contract list, one
  computed route, and one cargo plan.
- **Host** — the user who started the trip. Has full control
  (add/edit/remove contracts, change ship, end trip).
- **Crew** — anyone who joins via invite code. By default can view
  everything; host can grant edit rights.
- **Invite code** — short human-readable token (e.g. `GRX-7K9`). 6-9
  alphanumeric chars, ambiguous letters (`O`, `0`, `I`, `1`) avoided.
- **Solo mode** — no trip created; state lives only on the local
  machine. The default if the user never starts/joins a trip.

## Lifecycle

```
[App launch]
    │
    ├── "Hey Giant, start a trip"  ──► Host creates trip
    │       └── server returns invite code, e.g. GRX-7K9
    │
    ├── "Hey Giant, join trip GRX-7K9"  ──► Crew joins existing trip
    │
    └── (do nothing) ──► Solo mode, local-only state
```

A trip persists on the server until:
- The host ends it (`Hey Giant, end trip`), or
- 24 hours of zero activity (idle expiry), or
- 7 days max regardless of activity (hard expiry)

When a trip ends, the host is offered a local save of the final plan.

## Permissions

MVP roles:

| Role | View plan | Add contract | Edit/remove contract | Change ship | End trip |
|------|-----------|--------------|----------------------|-------------|----------|
| Host | ✓ | ✓ | ✓ | ✓ | ✓ |
| Crew (default) | ✓ | ✓ (own only) | ✓ (own only) | — | — |
| Crew (promoted) | ✓ | ✓ | ✓ | — | — |
| Viewer | ✓ | — | — | — | — |

Host can change anyone's role at any time.

## Real-time sync

- Every state change broadcasts to all connected clients within ~1 s.
- Plan recomputation runs server-side (one canonical answer for all
  participants) and the result streams down with each contract change.
- Optimistic local updates with server reconciliation if conflicts.

## Hosting

Recommended: **Supabase**.

- Postgres backs trips + contracts.
- Supabase Realtime channels handle the live sync.
- Anonymous JWT keyed on `trip_id` + role; RLS policies enforce permissions.
- Free tier covers personal/small-group use comfortably.
- Self-host option exists if we outgrow the managed service.

Alternatives considered:

- **Cloudflare Durable Objects** — excellent room model, but Workers
  lock-in and not native to a Python desktop app's stack.
- **Firebase Realtime DB** — easy, but Google lock-in and worse data
  model fit.
- **Self-hosted FastAPI + Redis pub/sub** — full control, but means
  *we* have to host infrastructure.

## Privacy

- No accounts, no email, no passwords stored.
- Trip data deletes on expiry. No analytics on trip contents.
- Invite codes are the *only* access control — share carefully.

## API surface (server)

```
POST   /trips                  Create trip (host)            → { trip_id, invite_code, host_token }
POST   /trips/join             Join via code                 → { trip_id, member_token, role }
GET    /trips/:id              Snapshot                      → { ship, contracts, plan, members }
POST   /trips/:id/contracts    Add contract
PATCH  /trips/:id/contracts/:c Edit contract
DELETE /trips/:id/contracts/:c Remove contract
PATCH  /trips/:id/ship         Change ship (host only)
PATCH  /trips/:id/members/:m   Promote/demote (host only)
DELETE /trips/:id              End trip (host only)
WS     /trips/:id/stream       Realtime feed
```

---

## Open questions

1. **Free hosting fits?** Supabase free tier = 500 MB DB, 2 GB egress,
   200 concurrent realtime connections. Plenty for a personal-scale
   tool — but we'll need to revisit if usage grows.
2. **Code length** — 6 chars (lower friction) or 9 chars (much harder
   to brute-force / collide)?
3. **Edit rights default for crew** — view-only (safer, host promotes
   trusted members) or own-contracts-only (more useful, no promotion
   needed)?

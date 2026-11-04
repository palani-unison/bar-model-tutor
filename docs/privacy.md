# Privacy: what Bar Model Tutor stores

The account holder is a parent or guardian (18 or over) who signs in with Google, agrees to the terms and
supervises their child. The built-in example lessons work without an account, and an anonymous lesson stores
nothing. Accounts exist so the tutor can remember how each child is doing, pace the next lesson to suit them,
and let the child replay their own lessons ("My lessons"); new problems (photos, typed questions) need a
signed-in member of a family whose parent has agreed to the terms.

## Stored (SQLite, `app/db.py`; on Modal, a persistent Volume)

| Table | Fields | Notes |
|---|---|---|
| `parents` | id, `google_sub`, display_name, created, consent_version, consented | `google_sub` is Google's stable account id (prefixed `google:`). display_name starts as the first name Google returns; the parent can change it ("What should we call you?"), and later Google sign-ins don't overwrite it. **No email**, no photo, no full name. consent_version and consented record which version of the terms (/terms) the parent agreed to, and when. |
| `children` | id, parent_id, nickname, level (P1–P6), avatar (an emoji), code_name, created, pw_hash, pw_ver | The nickname is whatever the parent types: we ask for a first name or nickname only, never a surname. code_name is the child's generated **hero name** ("Thor987"; profiles made earlier keep names like "brave-otter-42"). pw_hash is a **salted scrypt hash** of the simple password the parent sets for the child, never the password itself; the password is shown to the parent once, when set, and is never logged or returned again. pw_ver counts resets. No birthday, school or photo. |
| `attempts` | id, child_id, heuristic, level, checkpoint_key, outcome, seconds, created | One row per checkpoint in a signed-in child's lesson: the problem type (e.g. `before_after`), its level (e.g. `P5`), which step (`unit`, `answer`, `challenge`…), how it went (`first try`, `with hints`, `shown`, `early`, `missed`) and how many seconds it took. |
| `mastery` | child_id, heuristic, rating, n, avg_seconds, updated | One summary row per problem type: the rating, how many checkpoints it is based on, and the average answer time. |
| `lessons` | id, child_id, spec, solved, title, heuristic, level, thumb, steps, status, runs, solved_on, hints, seconds, progress, kept, created, updated | "My lessons": one row per lesson a **signed-in** child starts (a twin included), so they can replay it. `spec` is the checked bar-model plan, which **includes the question text**; `solved` is its worked-out values; `thumb` is up to four bar lengths for the tile picture. The rest is how it went (finished or not, how far it got, hints given, which try solved it, how long the last run took) and whether the child kept it (★). A replay updates the same row. At most 200 per child (the oldest that aren't kept go first). No photo, audio or transcript. |
| `plan_events` | id, created, parent_id, child_id, source, outcome, heuristic, planner, seconds, question, detail | One row per new problem planned, for the admin dashboard: whether it was verified, declined or failed, the method and how long it took. **The question text is kept only when the tutor declined or failed**, so it can be improved; a solved question is not kept here. |
| `flags` | id, created, parent_id, child_id, lesson_id, reason, note, question, spec, answer, status, admin_note, updated | "Report a problem": what went wrong (a fixed choice), the parent's or child's note, the question, the checked plan and the tutor's answer, and the owner's status and note. |
| `badges` | child_id, badge, earned_at | One row per method badge a child has earned (e.g. `remainder_ranger`, `came_back`) and when. Badges and the levels shown with them ("Starting" to "Master") are worked out from the `attempts` and `mastery` rows above; nothing else about the lesson is kept. |

Anonymous lessons can still earn a badge: it is shown at the end of that lesson and in the page for that visit
only, and is never sent to the database. "Try one like this" practice problems (twins) are made in server memory
from the lesson's own problem and kept in a small in-memory cache while the server runs; a twin is written to disk
only as a signed-in child's own lesson in `lessons`, once they start it.

## Never stored

- The question text, the bar model, the working or the answer of any problem in an anonymous lesson. (A
  signed-in child's own lessons keep the checked plan, which includes the question, in `lessons` above; a
  question the tutor couldn't do, or one you report, is kept in `plan_events` or `flags`.)
- Photos, PDFs or drawings (they are processed in memory and dropped, as before).
- Audio of the child's voice or the teacher's voice, and transcripts of what the child said or typed.
- The parent's email address or Google profile picture.
- A child's password in readable form (only its salted hash, above).
- Whether this device allowed sound or the microphone: that stays in the browser (`localStorage` keys
  `bmt.sound` and `bmt.mic`), so the "Turn on sound and microphone" step isn't asked again. The microphone
  permission is only for saying answers in a lesson; nothing said is recorded or saved.

## Cookies

- `bmt_session`: who is signed in on this browser (a parent id, or a child id with its family and password
  version). Signed (HMAC-SHA256), HttpOnly, SameSite=Lax, Secure on https; expires after 30 days or on
  sign-out. A child session made before the child's password was reset no longer counts.
- `bmt_family`: marks this browser as a family device after a parent has signed in with Google, so their
  children can pick their profile here without a password. Signed, HttpOnly, 180 days. "Sign out and forget
  this device" removes it.

## Kid sign-in

- On a **family device** (a parent signed in there) a child taps their avatar or types their hero name: no password.
- On **any other device** a child types their hero name and the password their parent set. Passwords aren't
  case-sensitive. Tries are limited (5 per hero name and 15 per device/network every 10 minutes); after that
  the child is asked to wait or ask their parent.
- **Parents can reset a child's password anytime** (family panel → "Password & link"). The old password stops
  working at once, and the child is signed out on every device that used it.
- The sign-in link `/k/<hero name>` only fills in the hero name on the sign-in screen. It is not a key: the
  password is still needed.

## Deleting

- **A child**: the parent panel's Delete button removes the child's profile (with its password hash) and every
  attempt, mastery, badge, lesson, report and plan-log row for that child at once (`DELETE /api/children/{id}`, cascading in the database).
- **Everything**: "Delete my account" removes the parent and all their children's data, saved lessons and reports included
  (`DELETE /api/me`).
- On Modal the deletion reaches the Volume with the next snapshot, within about five seconds.

## Who can see it

- A parent sees only their own children and their progress.
- A child sees only their own progress and their own lessons, once signed in (family device, or hero name and
  password). "My lessons" has no likes, followers, comments or sharing; another child's lesson id returns 404.
- A parent can list their own children's saved lessons (`GET /api/lessons?child_id=`); replaying one is the
  child's.
- The owner's `/admin` dashboard is open only to the owner's Google account (listed in `ADMIN_EMAILS`, checked at
  sign-in and never stored); there is no admin password. It shows usage over time, plan outcomes, each family's display
  name, children's nicknames, levels, progress and saved lessons, questions the tutor declined, and reports
  from families, so the owner can run the service and improve the tutor. It never shows emails, passwords,
  photos or audio.

## What is sent to AI models

- To Meta (Standard tier, never Contributor models): the question text or photo, the child's voice while the
  microphone is on, what the child types in a lesson, and a photo of their drawing when they ask
  "Check my bar model".
- To Fireworks AI (only when the fine-tuned Glimmer planner is on; it is off in the live app): the plain question text.
- To Resemble AI (only when the optional cloud teacher voice is on; it is off in the live app): the teacher's lines.
- **Never sent:** names, nicknames, hero names, emails, or progress.

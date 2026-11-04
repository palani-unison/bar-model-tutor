# Requirements

The original requirements live in the shared doc: https://claude.ai/artifact/JCD4TWTjG7P6jYisWTeU3H

Where the build landed (Nov 3): the core goal became a multimodal, real-time, personalised tutor rather than a
whiteboard video. In short:
- **Input:** camera, photo, PDF or typed word problems (P3–P6), with a question picker for full pages.
- **Verified maths:** every plan is checked by code (BarSpec v2) and a second model must agree, or the tutor
  declines.
- **Real-time lesson:** one WebSocket with a live board, a teacher's voice, streamed voice answers, interrupts
  and a code-enforced hint ladder; the child builds parts of the bar model themselves.
- **Personalised:** per-method rating and pacing, badges, twins, My lessons.
- **Safety and privacy:** a consenting parent (18+) holds the account; no names or progress go to a model;
  photos and audio are never stored. See [privacy.md](privacy.md).

The whiteboard video (FR-10) still exists (`POST /api/render`) but is no longer the main experience.

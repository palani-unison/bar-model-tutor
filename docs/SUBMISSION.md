# Bar Model Tutor

**Built by a father, for a father. It doesn't solve the homework. It draws it with you.**

Meta Global AI Developer Hackathon · Indie track · Palaniappan

Live app: https://jinboost--barmodel.modal.run · Code: https://github.com/palani-unison/bar-model-tutor

---

## The story

I'm Palaniappan. My daughter Valli learns maths the Singapore way: every word problem is solved with a *bar
model*, rectangles cut into equal units until the answer can be seen. I learned algebra. When Valli got stuck,
I could get the right number, but I couldn't show her the way her school wanted. Writing "let x be…" only
confused her more.

The apps I tried were answer machines. They print the answer, the homework gets done, and nothing is learned.
I didn't want a machine to do Valli's thinking. I wanted help to be the teacher sitting next to her: drawing
the bars, stopping to ask, waiting for her answer, giving a hint instead of the answer.

So I built that, for the two of us. And because I'm sure I'm not the only parent at the kitchen table who learned
maths differently from his child, I built it so that another father or mother can sit down with their child and
do the same.

## What it does

1. **Snap the question.** I take a photo of Valli's worksheet (or a PDF, or type it). If the page has several
   questions, it asks which one.
2. **The lesson starts at once.** While the AI plans, the teacher's voice already reads the question aloud and
   circles the numbers on the whiteboard. There is no waiting screen.
3. **The board draws, then stops and asks.** "Show me: how many units do Lily and Kavya each have?" Valli taps + and – to build the bars herself, drags the "?" to the bar we need to find,
   and answers "How much is 1 unit?" by voice.
4. **Wrong gets a hint, not the answer.** Two hints, then the teacher shows it. She can interrupt ("why four
   units?"), say "wait", or ask for the answer. The answer still only comes after she's tried, and that rule is
   code, not a prompt.
5. **Afterwards:** "Try one like this", the same kind of problem with new numbers, verified the same way. Method
   levels and badges ("Before–After Builder"). Every lesson is saved to *My lessons* to replay, private to our
   family.

I sign in with Google, agree to the terms, and add Valli. She gets a hero name and a password I can reset.
It runs in the browser on a laptop or tablet, or as an app on my phone.

## Not a chatbot, not a homework solver

- The child answers at every step; code checks each answer against a verified bar model.
- The answer appears only after the child gets it or has used two hints.
- Input is a photo, a voice and a finger on the board; output is a drawn, narrated whiteboard. There is no chat
  window.
- Every number is verified by code, and a second model must agree, before anything is drawn.

## How Meta AI is used

| Job | Model | Why this one |
| --- | --- | --- |
| Read the worksheet photo, plan the bar model, answer the child's "why?" | **Muse Spark 1.3** | Multimodal, tool calling, structured output |
| A second, independent answer that must agree | **Muse Spark 1.2** | A different checkpoint, so the two don't share mistakes |
| Escalation for the hardest problems | **Muse Wonder 1.0** | Only when Spark's plans keep failing the checks |
| A specialist planner, fine-tuned by me | **Muse Glimmer 30B + LoRA**, on Fireworks AI | Trained on bar-model plans; raced Spark under the same gate. As accurate but slower, so off in the live app |
| The child's voice, in real time | **Muse Voice Transcribe 1.0** | Streaming, so a spoken number is judged ~0.6 s after it's said |

Beyond a basic integration: models plan in parallel waves and race; each plan is a structured spec that code
verifies; a fine-tuned model was tested under the same gate; voice is streamed over a WebSocket with interruptions.
Standard tier only, never Contributor. No names, emails or progress are sent to any model.

## Technical depth

- **BarSpec v2.** The plan is not prose. It is a structured description of the bars, before/after stages,
  segments, and the working as equations.
- **Code checks the maths.** `verify_v2` solves the equations itself and rejects a plan whose numbers, bars or
  labels don't match the question. Then Muse Spark 1.2 answers independently, and the two must agree.
  If they can't, the tutor says it can't help with that one rather than risk teaching a wrong answer.
- **Checkpoints from verified data.** What the child is asked, and what counts as right (including building the
  bars and placing the "?"), comes from the verified spec, so a model never decides whether a child is right.
- **Real-time lesson.** One WebSocket carries board steps, the teacher's voice (Piper, offline text-to-speech),
  the child's streamed audio, barge-in and the hint ladder. The board starts teaching before the plan is ready.
- **Personalised.** An Elo-style rating per method (part–whole, before/after, same difference…) picks the pacing:
  challenge-first where she's strong, a full walk-through where she isn't.
- **Fine-tuning.** I trained Muse Glimmer with LoRA on Fireworks' serverless training to write bar-model plans
  and raced it against Spark under the same verification gate. Its plan won 42 of 100 races, but it made no
  lesson more accurate and the median plan slower, so the live app runs Spark alone. Measuring that, and
  switching it off, was the point of the test.

## Results

| Test | Result |
| --- | --- |
| Public word-problem set, all 107 | **107/107 correct, 0 wrong** |
| The first 100 of them again, with the fine-tuned Glimmer racing | 100/100 correct, 0 wrong; Glimmer's plan won 42 |
| School-style held-out set, 180 problems | 162 correct, the rest declined; 2 scored wrong: an answer-key error, and an answer given in cm where the key used m |
| 150 fresh school questions it had never seen (P4–P6, mostly 3+ marks) | 141 right once 4 wrong answer keys were fixed; 1 wrong, now declined |
| A hand-picked 300 hard ones (complex before–after, transfer, ratio change, tricky English) | 196/200 practice; **99/100 held back, 0 wrong** |
| Real scanned worksheet photos | 75/80 questions read exactly |
| Spoken answers through real-time speech recognition | 20/20 numbers understood |
| Adversarial "just tell me" / off-topic questions | 0 unsafe replies out of 124 |
| A full lesson in a headless browser, including a spoken answer | passes |
| Automated tests | 757 passing |

## Who it's for, and safety

The account holder is the parent, 18 or over, who agrees to the terms and sits with their child. Meta's Model API
terms require end users to be 18+, so this prototype is built around the parent: consent at sign-up, parent
supervision, and nothing personal sent to a model. We would not offer it directly to children without Meta's
written permission. Photos and audio are processed in memory and never stored.

## What's next

More bar-model templates (remainders, percentages, ratio changes), a parent's view of where their child needs
practice, and Mandarin and Tamil narration, so grandparents can sit at the table too.

---

*"AI explains. Code checks. The child thinks."*

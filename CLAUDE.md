# SC-Toolbox-Beta-V2

Python desktop app for Star Citizen (`main.py` / `skill_launcher.py`, PySide-style UI
under `ui/`, feature modules under `skills/` and `tools/`). Launched on Windows through
`LAUNCH.bat` / `SC_Toolbox.vbs`, which locate a Python interpreter rather than relying
on one being on `PATH`.

## Interrupt protocol

A message that arrives while a task is running is an interrupt, not a note to read later.

1. **Answer first.** When a user message arrives mid-task, stop before the next tool
   call and reply to it in plain text. Do not finish the current task first.
2. **Answer what was asked.** If it is a question, answer the question. If it is a
   correction, say what you are changing. Do not just silently adjust course.
3. **Then resume.** State in one line that you are going back to the task, and continue
   from where you stopped.

This rule outranks task momentum. "I was almost done" is not a reason to defer a reply.

### Why this file is not enough on its own

Claude Code **queues** a message typed while a turn is running — pressing `Enter` never
interrupts. The queued text is handed to the model as soon as the in-flight tool calls
finish, in the middle of the same turn, with no forced stop. By that point this file is
far back in context, so the rule loses out to the task in progress.

That is why the rule is also injected per-turn by the `UserPromptSubmit` hook in
`.claude/settings.json`: the hook attaches it to the prompt itself, so it arrives next
to the interrupting message instead of thousands of tokens upstream.

To interrupt for real rather than queue, press `Esc`. Claude Code stops the turn
immediately and sends anything queued right away.

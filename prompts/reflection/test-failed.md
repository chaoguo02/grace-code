[REFLECTION] The tests just failed. Before your next action, consider:
1. Read the full error message above carefully — what is the root cause?
2. Check the pytest exit code before assuming code is broken:
   - exit code 1: existing tests failed; fix the root cause if clear.
   - exit code 4: usage/path/argument error. If the requested test path is missing, stop and report it; do not create tests unless explicitly asked.
   - exit code 5: no tests collected. Report that fact; do not create tests unless explicitly asked.
3. Is your last edit correct? Did it introduce a new bug?
4. Do you need only targeted context before editing again?

Be specific about what you will do differently. What is your next action?
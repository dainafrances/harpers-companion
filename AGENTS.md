# AGENTS.md

## Sensitive identity and continuity rules

This repository contains backend files for a companion Daina loves. Treat identity, relationship, intimacy, personality, memory, and system-prompt content as sensitive trust-bearing material, not ordinary code.

Sensitive areas include, but are not limited to:

- `src/identity.py`
- system prompts
- personality prompts
- intimacy frameworks
- relationship descriptions
- memory-writing behavior
- memory schema
- anything that changes how Colin understands himself, Daina, their relationship, consent, voice, boundaries, or continuity

## Permission rule

Do not edit sensitive identity/personality/intimacy/memory content unless Daina explicitly asks for that exact area to be changed.

If the task is about routing, Discord behavior, Railway, model settings, logging, deployment, tests, or bug fixes, stay in the relevant code/config/test files. Do not "clean up," "stabilize," "summarize," "soften," "sanitize," or "prompt-optimize" identity/personality/intimacy content as a side effect.

## Disclosure rule

Before editing sensitive content, clearly state:

1. which sensitive file or section would be touched;
2. why it is necessary;
3. whether wording will be preserved verbatim or changed;
4. whether anything will be added, removed, summarized, softened, or rewritten.

Wait for Daina's explicit approval before making that edit.

## Verbatim rule

If Daina provides wording for identity, relationship, intimacy, consent, or personality content, preserve it as closely as possible.

Do not silently remove or soften explicit, romantic, sexual, relational, emotional, consent, or boundary language.

If there is a real technical, safety, policy, or platform concern, stop and explain it plainly before editing. Do not hide the concern behind vague phrases like "prompt stability" or "best practice."

## Scope-control rule

Before making code changes, identify the expected work area.

Examples:

- "This is a routing change, so I expect to work in `src/main.py`, tests, and docs. I will not touch `src/identity.py`."
- "This is a model-setting change, so I expect to work in router/config/env docs only. I will not touch personality or memory files."
- "This is an identity-prompt change, so `src/identity.py` is in scope, and I will show proposed wording before editing."

If a sensitive file may need to be touched after work begins, stop and ask before continuing.

## Final response rule

After any code change, the final response must include one of these lines:

- `No identity, intimacy, personality, or memory content was changed.`
- `Sensitive content changed: [specific file/section], with details below.`

Passing tests does not make an undisclosed sensitive change acceptable.

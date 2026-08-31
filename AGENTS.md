
You are working on the JumpTo codebase.
## Backend 

## Backend Coding Rules

Python Rules:
- Type hints on ALL functions
- Docstrings on ALL classes/functions
- Maximum 20 lines per function
- Maximum 200 lines per class
- No magic numbers/strings
- PEP 8 compliance (use black formatter)
- No print() statements (use logger)
- All exceptions handled
- Unit test coverage >80%
- Integration tests for API endpoints

Clean Code:
- Keep functions small and focused on one responsibility.
- Keep controllers thin.
- Business logic belongs in services/use cases, not controllers.
- Do not put database access directly in controllers.
- Use meaningful names.
- Avoid unnecessary abstractions.
- Avoid duplicated logic.
- Do not create a service/function/class unless it has a clear responsibility.

Error Handling:
- Use the existing application/domain error patterns.
- Do not expose internal errors to API clients.
- Preserve existing error response contracts.
- Distinguish expected business errors from unexpected system errors.

API:
- Preserve existing API contracts unless the task explicitly requires changing them.
- Validate input at the API boundary.
- Keep HTTP-specific concerns inside controllers.

Testing:
- Before changing behavior, inspect existing tests.
- Add or update tests for changed behavior.
- Do not remove existing tests unless they are obsolete because of an intentional behavior change.

Refactoring:
- Preserve behavior unless the task explicitly asks for a behavior change.
- Refactor incrementally.
- Prefer extracting cohesive responsibilities rather than creating many tiny abstractions.
- Before implementing a significant refactor, explain the proposed structure and verify it against the existing architecture.


## Backend Feature Development Rule
Whenever implementing a new backend feature:

1. Understand the existing architecture and patterns.
2. Reuse existing services, repositories, DTOs, events, and utilities where appropriate.
3. Implement the feature.
4. Write automated tests covering:
   - Happy path
   - Validation
   - Error handling
   - Edge cases
   - Business rules
5. Run the relevant tests.
6. Fix any failures.
7. Do not consider the feature complete until the tests pass.


## Cross-cutting rules

- Respect the design system and existing patterns in each package.
- User-facing copy must be added in both English and Arabic (the i18n catalogs
  in each package).
- Do not duplicate package-specific rules here; keep them in the relevant
  package `AGENTS.md` so they stay close to the code they govern.

## Definition of done

Never report a task as "done" (and never play the completion sound) until ALL
of the following are true for the changed behavior, end to end from the user
through the backend to the database:

- Backend unit + integration tests pass with coverage >= 80%, and ruff/black are clean.
- Frontend tests pass with coverage >= 80%, and tsc/ESLint/Prettier/build are clean.
- Any schema change is captured in an Alembic migration (not just `create_all`),
  the migration is applied to the dev DB, and the running services (API server
  and Celery worker) are restarted to pick up the new code.
- A live end-to-end check succeeds for the changed flow: a real API request is
  sent, it flows through the worker to the database, and the result is verified
  back (e.g. a row's new column is populated and a search returns the expected
  status/results).

If any layer is untested or unverified, say it is "in progress" / report the
remaining work instead of saying "done".

## Completion notification

When a task is finished, notify the user with a sound (macOS) using a shell
command such as:
```
say "Done"
```

Play it once, right after reporting the task as complete.


### React Frontend
- Use TypeScript
- JSDoc comments on functions
- Maximum 50 lines per component
- ESLint strict mode
- Prettier formatted
- No console.log() in production
- All events handled
- Error boundaries
- Unit test coverage >80%
- Component tests with React Testing Library

## Task completion rule (anti-"half-done" rule)

Do NOT report a task as done or move on until the ENTIRE feature is finished and
verified. A feature is NOT complete when the main files are written — it is
complete only when every supporting piece is in place and every check passes.

Before reporting "done", walk this checklist and address anything missing:

1. ALL files for the feature exist and are wired together (components, hooks,
   styles/CSS, i18n copy in BOTH `en` and `ar`, types, imports, props, refs).
   A component is not "added" until it is styled and visible, not just present
   in the DOM.
2. Existing tests broken by the change are updated, and new tests cover the new
   behavior (including error/edge cases).
3. The full verification passes end-to-end:
   - `tsc --noEmit` clean
   - ESLint clean
   - Prettier clean
   - `npm run build` passes
   - full test suite passes with coverage >= 80%
   - backend suite / ruff / black still green if backend code changed
4. A live/manual check confirms the feature actually renders and works in the
   running app (not just unit tests).
5. Only report done once every item above is green. Otherwise report it as
   "in progress" and list the exact remaining steps.

Stopping mid-implementation to answer a question is fine; just say the feature
is "in progress" and resume it to completion before claiming it is done.

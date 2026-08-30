
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

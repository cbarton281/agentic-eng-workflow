# Welcomepage Next.js Project Rules

## Question Mode
- When you see "#q" in a user query, this means that I don't want you to make any code changes, I just want you to answer my question.

## Project Architecture

### Core Technologies
- **Next.js 15+** with App Router and TypeScript
- **React 19** with functional components
- **FastAPI Backend** - All business logic, database operations, and external integrations
- **NextAuth.js** - Frontend session management only
- **Tailwind CSS** - Styling with Radix UI components
- **Vitest** - Testing framework with React Testing Library

### Architecture Principles
- **Next.js API routes are orchestration layer only** - No business logic, only session validation, JWT generation, and request proxying
- **All business logic lives in FastAPI backend** - Database operations, external integrations, complex business rules
- **Separation of concerns** - Frontend handles presentation and orchestration, backend handles all data and business logic
- **Progressive authentication** - Support both authenticated users (NextAuth) and anonymous users (cookie-based)

## Environment Variables

### Required Pattern
- **ALWAYS** use this exact pattern for FastAPI base URL:
  ```typescript
  const base = process.env.NEXT_PUBLIC_FASTAPI_BASE_URL;
  if (!base) throw new Error('NEXT_PUBLIC_FASTAPI_BASE_URL must be set');
  const FASTAPI_BASE_URL = `${base}/api`;
  ```

### Environment Variable Rules
- **NEVER** hardcode backend URLs or use localhost defaults
- **NEVER** use `process.env.NEXT_PUBLIC_FASTAPI_BASE_URL` directly without validation
- **ALWAYS** explicitly error if `NEXT_PUBLIC_FASTAPI_BASE_URL` is missing
- **NEVER** provide fallback values for environment variables (e.g., `|| 'localhost:3000'`)
- **ALWAYS** throw explicit errors when required environment variables are missing
- **ALWAYS** validate environment variables exist before use to catch misconfigurations immediately
- **ALWAYS** use `NEXT_PUBLIC_` prefix for environment variables accessed in client components
- **NEVER** use server-side environment variables (without `NEXT_PUBLIC_`) in client components
- **ALWAYS** use `NEXT_PUBLIC_WEBAPP_URL` for webapp base URL in client components (not NEXTAUTH_URL)

## Next.js API Routes

### Required Template Pattern
All API routes must follow this structure:

```typescript
import { NextRequest, NextResponse } from 'next/server';
import { validateSession } from '../../../../lib/validatesession';
import { signPublicJwt } from '../utils/jwt';
import { buildFastApiHeaders } from '../utils/fastapi-headers'; // REQUIRED: For Vercel bypass header
import { getOrCreateAnonymousUserData, setAnonymousUserCookies } from '../../../../lib/anonymousUserCookies';

// REQUIRED: Environment variable validation
const base = process.env.NEXT_PUBLIC_FASTAPI_BASE_URL;
if (!base) throw new Error('NEXT_PUBLIC_FASTAPI_BASE_URL must be set');
const FASTAPI_BASE_URL = `${base}/api`;

export async function GET(req: NextRequest) {
  try {
    // REQUIRED: Session validation
    const sessionResult = await validateSession(req);
    console.log('[API][endpoint][GET] Session result:', sessionResult);
    
    let publicId: string;
    let role: string;
    let teamPublicId: string;
    let anonymousUserData = null;

    if (sessionResult.isValid) {
      publicId = sessionResult.publicId!;
      role = sessionResult.role!;
      teamPublicId = sessionResult.teamPublicId!;
    } else {
      anonymousUserData = getOrCreateAnonymousUserData(req);
      publicId = anonymousUserData.publicUserId;
      teamPublicId = anonymousUserData.publicTeamId;
      role = 'PRE_SIGNUP';
    }

    // REQUIRED: JWT generation
    const jwt = signPublicJwt(publicId, role as any, undefined, teamPublicId);
    
    // REQUIRED: FastAPI call with proper headers (includes Vercel bypass header)
    const response = await fetch(`${FASTAPI_BASE_URL}/endpoint`, {
      method: 'GET',
      headers: buildFastApiHeaders({ jwt }), // REQUIRED: Always use buildFastApiHeaders() for bypass header
    });

    const data = await response.json();
    const nextResponse = NextResponse.json(data, { status: response.status });
    
    // REQUIRED: Set cookies for anonymous users
    if (anonymousUserData) {
      setAnonymousUserCookies(nextResponse, anonymousUserData);
    }
    
    return nextResponse;
  } catch (error) {
    console.error('[API][endpoint][GET] Error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
```

### Authentication Pattern
- **ALWAYS** use `validateSession()` for session validation
- **ALWAYS** generate JWTs with `signPublicJwt(publicId, role, teamId)`
- **ALWAYS** include Authorization header: `Authorization: Bearer ${jwt}`
- **ALWAYS** support both NextAuth sessions and anonymous cookie fallback
- **ALWAYS** handle anonymous users with `getOrCreateAnonymousUserData()`
- **ALWAYS** set anonymous user cookies with `setAnonymousUserCookies()` when needed

### FastAPI Call Restrictions
- **NEVER** make direct FastAPI calls from client-side components
- **ALWAYS** use Next.js API routes as proxies for FastAPI calls
- **ONLY** make FastAPI calls from server-side contexts:
  - Next.js API routes (`/app/api/`)
  - Middleware (`middleware.ts`)
  - Server components (App Router)
  - NextAuth authentication providers

### Vercel Protection Bypass Header (REQUIRED)
- **ALWAYS** use `buildFastApiHeaders()` from `app/api/wp/utils/fastapi-headers.ts` for ALL FastAPI calls
- **NEVER** manually construct headers for FastAPI calls - always use `buildFastApiHeaders()`
- **ALWAYS** import `buildFastApiHeaders` in API routes that call FastAPI
- The `buildFastApiHeaders()` function automatically includes the `x-vercel-protection-bypass` header using `VERCEL_AUTOMATION_BYPASS_SECRET`
- This header is **REQUIRED** to bypass Vercel's protection when calling the FastAPI layer
- In production, `buildFastApiHeaders()` will throw an error if `VERCEL_AUTOMATION_BYPASS_SECRET` is not set
- **NEVER** make fetch calls to FastAPI without using `buildFastApiHeaders()` - this will fail in production

### Client Component Pattern
- Client components must call Next.js API routes: `/api/endpoint`
- Use `fetch('/api/endpoint')` with `credentials: 'include'` for authentication
- Next.js API routes handle authentication and proxy to FastAPI

### Error Handling
- **ALWAYS** validate required environment variables exist before use
- **ALWAYS** throw clear errors if required env vars are missing
- **ALWAYS** use consistent error response patterns
- **ALWAYS** include comprehensive logging for debugging
- **ALWAYS** use proper HTTP status codes
- **ALWAYS** handle both success and error cases

### Logging Standards
- **ALWAYS** include comprehensive logging for debugging
- **ALWAYS** log publicId, role, and operation details
- **ALWAYS** use consistent logging patterns: `[API][endpoint][method]`
- **ALWAYS** log JWT generation and authentication steps

### Response Patterns
- **ALWAYS** use `NextResponse.json()` for API responses
- **ALWAYS** include proper HTTP status codes
- **ALWAYS** handle both success and error cases
- **ALWAYS** set anonymous user cookies when needed with `setAnonymousUserCookies()`

## Project Structure

### Directory Organization
- **`app/`** - Next.js App Router pages and routes
- **`app/api/`** - API routes (orchestration layer only)
- **`components/`** - Reusable React components
- **`components/ui/`** - Radix UI-based component library
- **`lib/`** - Utility functions and helpers
- **`hooks/`** - Custom React hooks
- **`types/`** - TypeScript type definitions
- **`__tests__/`** - Test files (unit, integration, flows)
- **`__tests__/flows/`** - Integration tests for user flows
- **`__tests__/test_descriptions/`** - Markdown descriptions of user flows

### File Naming Conventions
- Use **kebab-case** for file and directory names
- Use **PascalCase** for React component files (.tsx)
- Use **camelCase** for utility files and hooks (.ts)
- API route files should be named `route.ts` in their respective directories
- Test files should use `.test.ts` or `.test.tsx` extensions
- Use descriptive names that clearly indicate purpose

## React Component Guidelines

### Component Structure
- Use functional components with TypeScript
- Implement proper prop interfaces with clear type definitions
- Use React.forwardRef for components that need ref forwarding
- Prefer composition over inheritance
- Use proper key props for list items
- Implement proper cleanup in useEffect hooks
- Use React.memo for performance optimization when appropriate

### Client vs Server Components
- Use `"use client"` directive only when necessary for client-side functionality
- Prefer Server Components when possible for better performance
- Client components should only be used for:
  - Interactive elements (buttons, forms, modals)
  - Browser APIs (localStorage, window, etc.)
  - React hooks (useState, useEffect, etc.)
  - Event handlers

### State Management
- Use React hooks (useState, useEffect, useContext) for local state
- Use NextAuth for authentication state management
- Implement proper state lifting patterns
- Use custom hooks for complex state logic
- Avoid prop drilling by using context when appropriate
- Use proper dependency arrays in useEffect

## Styling and UI Guidelines

### Tailwind CSS
- Use Tailwind CSS for styling with custom design system
- Implement responsive design with mobile-first approach
- Use CSS custom properties for theming
- Follow consistent spacing and typography scales
- Use class-variance-authority for component variants

### Radix UI Components
- Use Radix UI components as base for complex UI elements
- Implement proper dark/light theme support
- Follow Radix UI accessibility patterns
- Use proper ARIA attributes

### Component Accessibility
- **ALWAYS** add labels to form inputs using `<Label>` component with `htmlFor`
- **ALWAYS** add `data-testid` attributes to interactive elements for testing
- **ALWAYS** use `aria-label` for buttons without visible text
- **ALWAYS** use proper semantic HTML elements
- **ALWAYS** ensure proper keyboard navigation
- **ALWAYS** use proper color contrast ratios
- **ALWAYS** implement proper focus management
- **ALWAYS** use proper alt text for images

### Form Inputs Pattern
```tsx
<Label htmlFor="input-id">Label Text</Label>
<Input
  id="input-id"
  data-testid="input-id"
  aria-label="Descriptive label"
  // ... other props
/>
```

### Buttons Pattern
```tsx
<Button
  onClick={handleClick}
  data-testid="button-name"
  aria-label="Button description"
>
  Button Text
</Button>
```

## Creating Testable UI Elements

### Principles
- **Prefer accessible queries** - Use semantic HTML and ARIA attributes first
- **Use test IDs as fallback** - Only when semantic queries aren't sufficient
- **Always include both** - Labels/ARIA for accessibility, test IDs for stable testing

### Priority Order
1. **High Priority**: Form inputs, buttons, modals, file uploads
2. **Medium Priority**: Navigation, toggles, dropdowns
3. **Lower Priority**: Display elements, layout containers (only if needed)

### Form Inputs Pattern (Required)
**ALWAYS** use `<Label>` with `htmlFor` for all form inputs:

```tsx
// ✅ CORRECT: Proper label association
<Label htmlFor="organization-name" className="text-xl font-semibold">
  Organization Name *
</Label>
<Input
  id="organization-name"
  type="text"
  placeholder="Enter your organization name"
  value={organizationName}
  onChange={(e) => setOrganizationName(e.target.value)}
  className="w-full"
  required
  data-testid="organization-name-input"
/>

// ❌ WRONG: No label
<h3>Organization Name *</h3>
<Input id="orgName" />
```

### Buttons Pattern (Required)
**ALWAYS** include `aria-label` and `data-testid`:

```tsx
// ✅ CORRECT: Button with icon (needs aria-label)
<Button
  onClick={handleRemoveLogo}
  aria-label="Remove logo"
  data-testid="remove-logo-button"
>
  <X className="h-4 w-4" />
</Button>

// ✅ CORRECT: Button with visible text
<Button
  onClick={handleContinue}
  data-testid="start-welcomepage-button"
  aria-label="Start your Welcomepage"
>
  Start your Welcomepage
</Button>
```

### File Upload Pattern (Required)
**ALWAYS** label file inputs and mark clickable areas:

```tsx
<Label htmlFor="logo-upload" className="text-xl font-semibold">
  Company Logo (Optional)
</Label>

{!logoUrl ? (
  <div
    onClick={() => fileInputRef.current?.click()}
    className="border-2 border-dashed rounded-lg p-8 text-center cursor-pointer"
    data-testid="logo-upload-area"
    role="button"
    aria-label="Upload company logo"
  >
    <Upload className="h-12 w-12 mx-auto mb-4" />
    <p>Click to upload your logo</p>
  </div>
) : (
  // ... existing logo display
)}

<input
  ref={fileInputRef}
  id="logo-upload"
  type="file"
  accept="image/*"
  className="hidden"
  data-testid="logo-file-input"
/>
```

### Selection Buttons Pattern (Required)
**ALWAYS** use `aria-pressed` for toggle/selection buttons:

```tsx
<button
  onClick={() => handleColorSchemeChange("corporate-blue")}
  aria-label="Corporate Blue color scheme"
  data-testid="color-scheme-corporate-blue"
  aria-pressed={selectedColorScheme === "corporate-blue"}
  className={cn(
    "relative p-3 rounded-xl border-2",
    selectedColorScheme === "corporate-blue"
      ? "border-blue-500 shadow-lg"
      : "border-gray-200"
  )}
>
  Corporate Blue
</button>
```

### Interactive Areas Pattern (Required)
**ALWAYS** mark clickable divs with proper roles:

```tsx
// ✅ CORRECT: Clickable div with proper role
<div
  onClick={() => setShowPromptSelector(true)}
  className="border-2 border-dashed rounded-2xl p-8 cursor-pointer"
  data-testid="add-prompt-empty-0"
  role="button"
  aria-label="Choose a prompt"
>
  Choose a prompt
</div>
```

### Naming Conventions

#### Test IDs
- Use **kebab-case**: `organization-name-input`
- Be **descriptive**: `remove-logo-button` not `remove-btn`
- Include **context**: `color-scheme-corporate-blue` not `blue`
- Match **functionality**: `start-welcomepage-button` not `submit`

#### IDs
- Match test IDs when possible: `id="organization-name"` matches `data-testid="organization-name-input"`
- Use **kebab-case** consistently
- Be **unique** within the page/component

### Testing Query Priority

When writing tests, use queries in this order:

1. **`getByRole`** - Best for buttons, inputs, headings
   ```tsx
   screen.getByRole('button', { name: /start your welcomepage/i })
   ```

2. **`getByLabelText`** - Best for form inputs with labels
   ```tsx
   screen.getByLabelText(/organization name/i)
   ```

3. **`getByPlaceholderText`** - For inputs without labels
   ```tsx
   screen.getByPlaceholderText(/enter your organization name/i)
   ```

4. **`getByText`** - For visible text content
   ```tsx
   screen.getByText(/click to upload your logo/i)
   ```

5. **`getByTestId`** - Last resort, when other queries don't work
   ```tsx
   screen.getByTestId('organization-name-input')
   ```

### Component Checklist

When creating or updating a component, ensure:

- [ ] All form inputs have `<Label>` with `htmlFor`
- [ ] All inputs have `id` matching label's `htmlFor`
- [ ] All inputs have `data-testid` for fallback
- [ ] All buttons have `aria-label` (if no visible text) or descriptive text
- [ ] All buttons have `data-testid` for fallback
- [ ] File inputs have labels and test IDs
- [ ] Interactive areas (clickable divs) have `role="button"` and `aria-label`
- [ ] Selection/toggle buttons have `aria-pressed` and test IDs
- [ ] Modal triggers have test IDs
- [ ] Modal content has test IDs for key elements

### Benefits

✅ **Better Accessibility** - Screen readers can properly identify form fields  
✅ **Easier Testing** - Tests can use semantic queries instead of fragile selectors  
✅ **More Maintainable** - Tests won't break when text changes  
✅ **Better UX** - Proper labels improve form usability

## Testing Guidelines

### Testing Framework
- Use **Vitest** for test runner
- Use **React Testing Library** for component testing
- Use **@testing-library/user-event** for user interactions
- Use **jsdom** for DOM environment

### Test Organization
- Unit tests: `__tests__/lib/` for utilities
- Component tests: `__tests__/components/` for components
- Integration tests: `__tests__/flows/` for user flows
- API tests: `__tests__/api/` for API route tests
- Test descriptions: `__tests__/test_descriptions/` (Markdown)

### Testing Patterns
- **ALWAYS** use `getByRole`, `getByLabelText`, `getByPlaceholderText` before `getByTestId`
- **ALWAYS** add `data-testid` to elements that need stable selectors
- **ALWAYS** test user interactions, not implementation details
- **ALWAYS** test both success and error scenarios
- **ALWAYS** mock external dependencies (fetch, NextAuth, etc.)
- **ALWAYS** use `screen` from React Testing Library
- **ALWAYS** use `userEvent` for simulating user interactions

### Test File Structure
```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Component } from './component';

describe('Component', () => {
  beforeEach(() => {
    // Setup
  });

  it('should render correctly', () => {
    render(<Component />);
    expect(screen.getByRole('button')).toBeInTheDocument();
  });

  it('should handle user interaction', async () => {
    const user = userEvent.setup();
    render(<Component />);
    await user.click(screen.getByRole('button'));
    // Assertions
  });
});
```

### User Flow Testing
- Create Markdown descriptions in `__tests__/test_descriptions/` first
- Write integration tests in `__tests__/flows/` based on descriptions
- Test complete user journeys, not just individual components
- Mock API responses appropriately
- Test navigation and state changes

## Form Handling

### React Hook Form
- Use react-hook-form for form management
- Implement proper form validation with Zod
- Use proper error handling and user feedback
- Implement proper form state management
- Use proper accessibility attributes
- Handle both controlled and uncontrolled components

### Validation Pattern
```typescript
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';

const schema = z.object({
  name: z.string().min(1, 'Name is required'),
});

const form = useForm({
  resolver: zodResolver(schema),
});
```

## Error Handling

### Error Boundaries
- Implement proper error boundaries for React components
- Use proper error logging and monitoring
- Handle both client and server-side errors
- Provide meaningful error messages to users
- Implement proper fallback UI for errors

### API Error Handling
- Always catch errors in try-catch blocks
- Log errors with context: `[API][endpoint][method] Error:`
- Return appropriate HTTP status codes
- Provide clear error messages in responses

## Security Best Practices

### Input Validation
- Validate all user inputs
- Use Zod schemas for validation
- Sanitize user-generated content
- Never trust client-side validation alone

### Authentication
- Use proper CSRF protection
- Implement proper authentication checks
- Use secure headers and CORS policies
- Never expose sensitive data in client components
- Use environment variables for sensitive data

### API Security
- Always validate sessions before processing requests
- Always include JWT tokens in FastAPI requests
- Never make direct FastAPI calls from client
- Always use HTTPS in production
- Implement proper rate limiting

## Performance Optimization

### Next.js Optimizations
- Use Next.js Image component for images
- Implement proper code splitting
- Use dynamic imports for heavy components
- Implement proper caching strategies
- Use proper loading states and suspense

### React Optimizations
- Use React.memo for expensive components
- Optimize bundle size with proper imports
- Use proper dependency arrays in useEffect
- Avoid unnecessary re-renders

## Code Quality Standards

### TypeScript
- Use TypeScript strict mode
- Use proper type definitions
- Avoid `any` types - use proper types or `unknown`
- Use proper JSDoc comments for complex functions
- Define interfaces for all data structures

### Code Style
- Implement proper ESLint rules
- Use Prettier for code formatting
- Follow consistent naming conventions
- Keep functions focused and single-purpose
- Use meaningful variable and function names

### Documentation
- Document complex business logic
- Add JSDoc comments for public functions
- Keep README files updated
- Document API contracts
- Document user flows in test descriptions

## Deployment

### Environment Configuration
- Use proper environment variables
- Never commit sensitive data
- Use different configs for dev/staging/prod
- Validate all required env vars at startup

### Build Optimization
- Implement proper build optimization
- Use proper deployment configuration
- Monitor bundle size
- Use proper error tracking
- Implement proper performance monitoring

## Enforcement

These rules must be applied to ALL code in the project:
- Any new API routes must follow the template pattern exactly
- Any new components must include proper labels and test IDs
- Any new tests must follow the testing patterns
- Any environment variable usage must follow the validation pattern
- Any FastAPI calls must go through Next.js API routes

When in doubt, refer to existing code patterns in the codebase for consistency.

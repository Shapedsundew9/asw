# UI Development Standards

## Theme

- Default to a Dark Theme for all user interfaces.
- Provide a Light Theme alternative where feasible.

## Accessibility

- Use accessible contrast ratios (minimum WCAG AA: 4.5:1 for normal text, 3:1 for large text).
- Ensure all interactive elements are keyboard-navigable.
- Provide meaningful `alt` text for images and `aria-label` attributes for interactive elements.

## Responsive Design

- Design mobile-first, then scale up to larger viewports.
- Use relative units (`rem`, `%`, `vh`/`vw`) over fixed pixels where appropriate.
- Test layouts at common breakpoints: 320px, 768px, 1024px, 1440px.

## Typography

- Use a system font stack for body text to minimise load times.
- Maintain a clear type hierarchy: headings, subheadings, body, captions.
- Minimum body text size: 16px (1rem).

## Consistency

- Reuse component patterns; do not create one-off UI elements for common interactions.
- Follow the project's established colour palette and spacing scale.
- Keep visual noise low — favour whitespace over decoration.

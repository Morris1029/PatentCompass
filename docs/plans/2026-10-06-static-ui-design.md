# HumanIP Static UI Design

## Scope

Build a desktop-first, Chinese-language HTML prototype from the product specification. Keep the original specification unchanged. Use plain HTML, shared CSS and local SVG assets, without JavaScript, frameworks, remote fonts or backend connections.

## Pages

- `prototype/index.html`: project overview and recent sample reviews.
- `prototype/upload.html`: mixed-material intake and material-role confirmation.
- `prototype/review.html`: document, review findings and version timeline.
- `prototype/contribution.html`: human action, version change and final expression.
- `prototype/citations.html`: citation statements, sources and verification evidence.
- `prototype/visual.html`: image versions, comparison and contribution evidence.
- `prototype/report.html`: review summary, uncertainties and next actions.
- `prototype/report-print.html`: printable report preview.

## Design Direction

An evidence workspace with a navy navigation rail, warm paper background, white panels, blue primary actions and restrained teal accents. Source categories and review status use different visual treatments. Turnitin informs the review workspace; Grammarly informs attribution highlights; scite informs citation cards; Adobe Inspect informs visual comparison.

## Verification

1. Confirm every page links to the other main pages and the overview.
2. Parse all HTML files and verify local links, fragments, styles and image assets resolve.
3. Preview the overview and dense review pages at desktop and narrow widths, when a rendering runtime is available.
4. Mark all review results as fictional sample data. Unavailable actions must be visibly disabled; working controls are local links or native HTML disclosures.

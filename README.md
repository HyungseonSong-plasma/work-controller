# work-controller
Issue-based work management and PR-based work-unit orchestration

## Repository layout

- `.github/ISSUE_TEMPLATE/`: GitHub-native issue forms (reserved path).
- `.github/workflows/`: GitHub-native event triggers and contract enforcement (reserved path).
- `validation/`: reusable validation functions and their tests, independent of GitHub event handling.

Run `python3 validation/validate_issue.py --test` to test the Work issue validator.

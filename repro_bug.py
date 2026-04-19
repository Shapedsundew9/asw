import re

_CHECKED_RE = re.compile(r"^\s*[\-*+]\s+\[[xX]\]\s+.+", re.MULTILINE)

content = """
## Acceptance Criteria Checklist
*   [x] The tree model renders correctly without missing textures or geometry.
*   [x] Mouse-drag or touch-drag successfully orbits the camera 360 degrees around the tree.
"""

if not _CHECKED_RE.search(content):
    print("FAILED: No completed checklist items found.")
else:
    print("PASSED: Completed checklist items found.")

# Test with single space
content_single_space = """
## Acceptance Criteria Checklist
* [x] The tree model renders correctly.
"""
if not _CHECKED_RE.search(content_single_space):
    print("FAILED (single space): No completed checklist items found.")
else:
    print("PASSED (single space): Completed checklist items found.")

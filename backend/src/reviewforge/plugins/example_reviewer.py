"""Example Plugin Reviewer — demonstrates how to write a custom reviewer.

Copy this file and modify to create your own reviewer. The plugin loader
will auto-discover any .py file in this directory that contains a class
inheriting from BaseReviewer with `plugin_name` and `plugin_type` attributes.
"""

from reviewforge.engine.reviewers import BaseReviewer


class ExampleReviewer(BaseReviewer):
    """Example: checks for TODO/FIXME comments in changed code."""

    plugin_name = "example_reviewer"
    plugin_type = "maintenance"

    def __init__(self, llm, registry, gateway):
        super().__init__(
            name=self.plugin_name,
            reviewer_type=self.plugin_type,
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=4,
        )

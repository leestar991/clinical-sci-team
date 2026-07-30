"""cellflow_community.skills package — T-SKILL-16 emptied this module.

The Local/Remote/Tos/Oss SkillStorage variants were folded into the
unified :class:`deerflow.skills.storage.manifest_skill_storage.ManifestSkillStorage`
which routes IO through the configured StorageBackend instead of holding
its own filesystem / SDK client. This package no longer exports anything
public; keep the module around so an old import doesn't break at
collection time, but it stays empty.
"""

__all__: list[str] = []

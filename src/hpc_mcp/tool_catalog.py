"""Daily batch tools and an explicit advanced compatibility catalog."""

TOOL_PROFILES = ('batch', 'advanced')

# Backend methods and CLI commands remain available in both profiles. These MCP
# entrypoints expose alternative manual pipelines and are omitted from daily use.
ADVANCED_TOOLS = frozenset({
    'application_prepare',
    'job_lifecycle_start', 'job_lifecycle_get', 'job_lifecycle_list',
    'job_lifecycle_pause', 'job_lifecycle_resume', 'job_lifecycle_tick',
    'workflow_plan', 'workflow_review', 'workflow_start', 'workflow_pause',
    'workflow_retry', 'workflow_tick',
    'monitor_watch', 'monitor_unwatch',
    'profile_get', 'profile_list',
    'script_generate', 'job_prepare_generated',
    'template_import', 'template_list', 'template_get', 'template_plan', 'template_run',
    'job_prepare', 'job_submission_plan', 'job_authorize', 'job_submit', 'job_status',
    'job_sync_preview', 'job_sync_start', 'job_sync',
    'job_cache_cleanup', 'job_storage_cleanup', 'job_remote_cleanup', 'input_cache_cleanup',
})


def validate_profile(profile):
    if profile not in TOOL_PROFILES:
        raise ValueError('tool_profile must be batch or advanced')
    return profile

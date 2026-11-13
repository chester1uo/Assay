"""Request-local research workspace identity; never derive it from agent input."""
from contextvars import ContextVar

# None means an explicitly local/stdio caller or the shared service principal.
# HTTP authentication failures use a distinct sentinel so they cannot fall back
# to the shared workspace if a route accidentally omits authentication.
UNAUTHENTICATED = 'unauthenticated'
current_tenant = ContextVar('assay_research_tenant', default=None)


def tenant_for_user(user):
    if user is None:
        return UNAUTHENTICATED
    if user.id == 0:
        return None  # configured service keys intentionally share the service workspace
    if not user.workspace_id:
        raise ValueError('Account is missing its persistent workspace identity')
    return f'account:{user.workspace_id}'

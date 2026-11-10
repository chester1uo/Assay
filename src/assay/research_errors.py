"""Machine-readable recovery information for research execution."""
class ResearchError(ValueError):
    def __init__(self, code, message, *, retryable=False, action='inspect_experiment'):
        super().__init__(message)
        self.code, self.retryable, self.action = code, retryable, action

    def to_dict(self):
        return {'type':type(self).__name__, 'code':self.code, 'message':str(self),
                'retryable':self.retryable, 'recommended_action':self.action}


def recovery_error(exc):
    if isinstance(exc, ResearchError):
        return exc.to_dict()
    return {'type':type(exc).__name__, 'code':'BACKEND_UNAVAILABLE', 'message':str(exc),
            'retryable':True, 'recommended_action':'reconcile_experiment'}

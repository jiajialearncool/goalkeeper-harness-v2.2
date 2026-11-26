from pathlib import Path

from ..sandbox import Scope, Seatbelt, PermissionDenied
from .files import safe_file, workspace_secret_rules


class WorkspaceScope(Scope):
    def path(self, relative, write=False):
        try:
            safe_file(Path(self.workspace), relative)
        except ValueError as error:
            raise PermissionDenied(str(error)) from error
        return super().path(relative, write)


class PreviewSeatbelt(Seatbelt):
    def profile(self):
        return super().profile() + '\n' + workspace_secret_rules(self.root)

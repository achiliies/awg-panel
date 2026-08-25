"""Print what the panel's account is called.

`awg-panel passwd` and `awg-panel reset-2fa` have to name an account, and until
the panel could be renamed from Settings they simply assumed "admin". An admin
who renames themselves and then forgets the new password would find the one
documented way back in refusing to run - so the shell wrapper asks here instead
of assuming, and this answers for the ordinary case of a panel with one account.

Deliberately silent about anything else on the account. It prints a name that
the login form already accepts and nothing beside it.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Print the panel account's username. Fails unless there is exactly one account."

    def handle(self, *args: object, **options: object) -> None:
        user_model = get_user_model()
        field = user_model.USERNAME_FIELD
        # One more than is needed to answer, which is what tells "the one
        # account" from "the first of several" without counting the table.
        names = sorted(user_model.objects.values_list(field, flat=True)[:20])

        if not names:
            raise CommandError(
                "The panel has no account yet. The installer creates one; "
                "'manage.py seedadmin' is what it calls to do it."
            )
        if len(names) > 1:
            listed = ", ".join(names)
            raise CommandError(
                f"There is more than one account ({listed}), so name the one you mean, "
                f"for example: awg-panel passwd {names[0]}"
            )

        self.stdout.write(names[0])

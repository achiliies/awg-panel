"""Create the first admin account, once - or reset the one already there.

install-panel.sh runs this on every fresh install and must not have to know
whether the database is new, so the command is idempotent and says which of the
things it did in one word on stdout.

--reset is the second half of that. The account lives in /var/lib and outlives
every reinstall of the code in /opt, so re-running the installer over a panel
whose password has been lost used to be no help at all: seedadmin saw an
account, left it alone, and the operator was still locked out. With --reset the
installer's own questions can rename that account and set a new password on it,
which is the one recovery path that does not need the panel to be reachable
first.
"""

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

ENV_PASSWORD = "AWG_PANEL_ADMIN_PASSWORD"


class Command(BaseCommand):
    help = "Create the panel's first admin account, or reset it with --reset."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--username", default="admin", help="Account to create (default: admin)"
        )
        parser.add_argument(
            "--password",
            default="",
            help=f"Password for the new account. Read from ${ENV_PASSWORD} if omitted.",
        )
        parser.add_argument("--email", default="", help="Optional, only used for password resets")
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Rename the existing account to --username and set --password on it, "
                "instead of leaving it alone. Refused unless there is exactly one account."
            ),
        )

    def handle(self, *args: object, **options: object) -> None:
        user_model = get_user_model()
        username = str(options["username"]).strip()
        password = str(options["password"]) or os.environ.get(ENV_PASSWORD, "")

        # "Any user", not "this username": a second account created by hand is
        # still an admin, and re-running the installer must not quietly add
        # another one or reset the password of the one already in use.
        if user_model.objects.exists():
            if not options["reset"]:
                self.stdout.write("exists")
                self.stderr.write(
                    "An account already exists, so no new one was created. "
                    "Reset its password with 'sudo awg-panel passwd'."
                )
                return
            self._reset(user_model, username, password)
            return

        if not username:
            raise CommandError("--username cannot be empty.")
        if not password:
            raise CommandError(
                f"No password given. Pass --password, or put one in ${ENV_PASSWORD}."
            )
        self._validate(password)

        with transaction.atomic():
            user_model.objects.create_superuser(
                username=username,
                email=str(options["email"]),
                password=password,
            )

        self.stdout.write("created")

    def _reset(self, user_model: type, username: str, password: str) -> None:
        """Rename and re-key the single existing account."""
        field = user_model.USERNAME_FIELD
        # The same rule accountname applies, and for the same reason: with more
        # than one account there is no "the" account to reset, and picking one
        # would be picking somebody's login to overwrite.
        accounts = list(user_model.objects.order_by("pk")[:2])
        if len(accounts) > 1:
            raise CommandError(
                "There is more than one account, so --reset has no single one to act on. "
                "Use 'sudo awg-panel passwd <user>' to change a named account."
            )

        account = accounts[0]
        if not username:
            username = getattr(account, field)
        if password:
            self._validate(password)

        with transaction.atomic():
            setattr(account, field, username)
            if password:
                account.set_password(password)
            # An account that can no longer sign in to the panel is not one this
            # command has finished resetting: an install that has been demoted
            # or switched off by hand is exactly the state somebody re-runs the
            # installer to get out of.
            account.is_active = True
            account.is_staff = True
            account.is_superuser = True
            account.save()

        self.stdout.write("reset")

    def _validate(self, password: str) -> None:
        # Nothing to reject while AUTH_PASSWORD_VALIDATORS is empty, which it is:
        # the account's owner chooses its password. Kept so a deployment that
        # puts rules back in that setting gets them here too, and gets told why
        # the install stopped rather than watching the command fail obscurely.
        try:
            validate_password(password)
        except DjangoValidationError as exc:
            raise CommandError("That password was rejected: " + " ".join(exc.messages)) from exc

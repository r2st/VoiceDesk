"""Registration, sign-in, refresh-token rotation and team management.

``test_security`` covers the primitives — hashing, JWT encode/decode. This
module covers what sits on top of them: the rules that decide who gets a
token, how long it stays usable, and what happens to every other session when
a password changes or an account is closed.

The refresh-rotation tests are the load-bearing ones. A refresh token is a
long-lived bearer credential, so the interesting cases are not the happy path
but the ones where a token is presented twice — which is what a stolen token
looks like from the server's side.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError, ConflictError, NotFoundError, ValidationError
from app.core.security import create_access_token, create_refresh_token, hash_password, hash_token
from app.core.throttle import MAX_ATTEMPTS_PER_ACCOUNT, WINDOW_SECONDS, LoginThrottleError
from app.models.business import Business, RefreshToken, User
from app.models.enums import BusinessStatus, PlanTier, UserRole
from app.schemas.auth import LoginRequest, RegisterRequest, UserCreate, UserUpdate
from app.services import auth_service
from tests.conftest import auth_headers

#: The password every fixture user is created with.
FIXTURE_PASSWORD = "Sup3rSecret!"

#: The shared fixtures use ``@sunrise.test``. ``.test`` is a reserved TLD that
#: ``EmailStr`` refuses, so anything that travels through a request schema —
#: login, invites, registration — needs an address that could really exist.
DOMAIN = "sunrisediagnostics.in"


def register_payload(**overrides: Any) -> RegisterRequest:
    data: dict[str, Any] = {
        "business_name": "Lotus Dental Care",
        "business_phone": "9876500001",
        "industry": "healthcare",
        "city": "Pune",
        "state": "Maharashtra",
        "full_name": "Anita Desai",
        "email": "anita@lotusdental.in",
        "password": "L0tusDental!",
    }
    data.update(overrides)
    return RegisterRequest(**data)


async def refresh_rows(session: AsyncSession, user_id: uuid.UUID) -> list[RefreshToken]:
    result = await session.execute(
        select(RefreshToken)
        .where(RefreshToken.user_id == user_id)
        .order_by(RefreshToken.created_at)
    )
    return list(result.scalars().all())


async def make_user(
    session: AsyncSession,
    business: Business,
    email: str,
    role: UserRole,
    *,
    password: str = FIXTURE_PASSWORD,
) -> User:
    user = User(
        business_id=business.id,
        email=email,
        full_name=email.split("@")[0].title(),
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def account(session: AsyncSession, business: Business) -> User:
    """An owner who can actually be typed into a sign-in form."""
    return await make_user(session, business, f"meera@{DOMAIN}", UserRole.OWNER)


@pytest_asyncio.fixture
async def colleague(session: AsyncSession, business: Business) -> User:
    """A second, disposable member of the same tenant."""
    return await make_user(session, business, f"reception@{DOMAIN}", UserRole.VIEWER)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
class TestRegistration:
    async def test_creates_a_trial_tenant_with_an_owner(self, session: AsyncSession):
        business, user, tokens = await auth_service.register_business(session, register_payload())

        assert business.status == BusinessStatus.TRIAL
        assert business.trial_ends_at is not None
        assert business.trial_ends_at > datetime.now(UTC) + timedelta(days=13)
        assert user.role == UserRole.OWNER
        assert user.business_id == business.id
        assert user.is_active
        assert tokens.access_token and tokens.refresh_token

    async def test_password_is_hashed_not_stored(self, session: AsyncSession):
        _, user, _ = await auth_service.register_business(session, register_payload())

        assert "L0tusDental!" not in user.password_hash
        assert user.password_hash.startswith("$2")

    async def test_email_is_stored_lowercased(self, session: AsyncSession):
        _, user, _ = await auth_service.register_business(
            session, register_payload(email="Anita.Desai@LotusDental.IN")
        )
        assert user.email == "anita.desai@lotusdental.in"

    async def test_duplicate_email_is_rejected_across_tenants(self, session: AsyncSession):
        await auth_service.register_business(session, register_payload())

        with pytest.raises(ConflictError):
            await auth_service.register_business(
                session, register_payload(business_name="Another Clinic")
            )

    async def test_same_business_name_gets_a_distinct_slug(self, session: AsyncSession):
        first, _, _ = await auth_service.register_business(session, register_payload())
        second, _, _ = await auth_service.register_business(
            session, register_payload(email=f"second@{DOMAIN}")
        )

        assert first.slug != second.slug
        assert second.slug.startswith("lotus-dental-care")

    def test_slugify_strips_punctuation_and_case(self):
        assert auth_service.slugify("Dr. Rao's  ENT & Clinic!") == "dr-rao-s-ent-clinic"

    def test_slugify_falls_back_when_nothing_survives(self):
        # A Devanagari-only name leaves no ASCII to slugify; a blank slug would
        # collide with every other blank one, so it becomes "business".
        assert auth_service.slugify("आरोग्य") == "business"

    async def test_register_endpoint_returns_a_usable_token(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/register", json=register_payload().model_dump(mode="json")
        )
        assert response.status_code == 201
        body = response.json()

        me = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {body['tokens']['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json()["email"] == "anita@lotusdental.in"
        assert me.json()["role"] == "owner"

    async def test_register_never_echoes_the_password(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/register", json=register_payload().model_dump(mode="json")
        )
        assert "L0tusDental!" not in response.text

    @pytest.mark.parametrize(
        "password",
        ["short1!", "alllettershere", "1234567890123"],
        ids=["too-short", "letters-only", "digits-only"],
    )
    async def test_weak_passwords_are_rejected(self, client: AsyncClient, password: str):
        payload = register_payload().model_dump(mode="json") | {"password": password}
        response = await client.post("/api/v1/auth/register", json=payload)
        assert response.status_code == 422

    async def test_duplicate_registration_is_a_conflict_over_http(self, client: AsyncClient):
        body = register_payload().model_dump(mode="json")
        assert (await client.post("/api/v1/auth/register", json=body)).status_code == 201

        second = await client.post("/api/v1/auth/register", json=body)
        assert second.status_code == 409


# --------------------------------------------------------------------------- #
# Sign-in
# --------------------------------------------------------------------------- #
class TestLogin:
    async def test_valid_credentials_issue_a_pair(self, session: AsyncSession, account: User):
        user, tokens = await auth_service.authenticate(
            session, LoginRequest(email=account.email, password=FIXTURE_PASSWORD)
        )

        assert user.id == account.id
        assert tokens.access_token != tokens.refresh_token
        assert tokens.expires_in > 0

    async def test_login_is_case_insensitive_on_email(self, session: AsyncSession, account: User):
        user, _ = await auth_service.authenticate(
            session, LoginRequest(email=account.email.upper(), password=FIXTURE_PASSWORD)
        )
        assert user.id == account.id

    async def test_login_stamps_last_seen(self, session: AsyncSession, account: User):
        assert account.last_login_at is None
        await auth_service.authenticate(
            session, LoginRequest(email=account.email, password=FIXTURE_PASSWORD)
        )
        assert account.last_login_at is not None

    async def test_wrong_password_is_rejected(self, session: AsyncSession, account: User):
        with pytest.raises(AuthenticationError):
            await auth_service.authenticate(
                session, LoginRequest(email=account.email, password="not-the-password")
            )

    async def test_unknown_email_is_rejected(self, session: AsyncSession):
        with pytest.raises(AuthenticationError):
            await auth_service.authenticate(
                session, LoginRequest(email=f"nobody@{DOMAIN}", password=FIXTURE_PASSWORD)
            )

    async def test_unknown_email_and_wrong_password_give_the_same_message(
        self, session: AsyncSession, account: User
    ):
        # Distinguishable messages would turn the login form into an oracle for
        # which addresses hold accounts.
        with pytest.raises(AuthenticationError) as unknown:
            await auth_service.authenticate(
                session, LoginRequest(email=f"nobody@{DOMAIN}", password="whatever1!")
            )
        with pytest.raises(AuthenticationError) as wrong:
            await auth_service.authenticate(
                session, LoginRequest(email=account.email, password="whatever1!")
            )
        assert str(unknown.value) == str(wrong.value)

    async def test_deactivated_user_cannot_sign_in(self, session: AsyncSession, account: User):
        account.is_active = False
        await session.flush()

        with pytest.raises(AuthenticationError, match="deactivated"):
            await auth_service.authenticate(
                session, LoginRequest(email=account.email, password=FIXTURE_PASSWORD)
            )

    async def test_cancelled_business_cannot_sign_in(
        self, session: AsyncSession, business: Business, account: User
    ):
        business.status = BusinessStatus.CANCELLED
        await session.flush()

        with pytest.raises(AuthenticationError, match="cancelled"):
            await auth_service.authenticate(
                session, LoginRequest(email=account.email, password=FIXTURE_PASSWORD)
            )

    async def test_suspended_business_can_still_sign_in(
        self, session: AsyncSession, business: Business, account: User
    ):
        # Suspension pauses calling, not access: the owner has to be able to
        # get in and settle the bill that lifts the suspension.
        business.status = BusinessStatus.SUSPENDED
        await session.flush()

        user, _ = await auth_service.authenticate(
            session, LoginRequest(email=account.email, password=FIXTURE_PASSWORD)
        )
        assert user.id == account.id

    async def test_soft_deleted_business_cannot_sign_in(
        self, session: AsyncSession, business: Business, account: User
    ):
        business.deleted_at = datetime.now(UTC)
        await session.flush()

        with pytest.raises(AuthenticationError):
            await auth_service.authenticate(
                session, LoginRequest(email=account.email, password=FIXTURE_PASSWORD)
            )

    async def test_slug_disambiguates_one_email_in_two_tenants(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        """A consultant working for two tenants reuses one address."""
        shared = "shared@consultant.in"
        await make_user(session, business, shared, UserRole.ADMIN, password="FirstPass1!")
        await make_user(session, other_business, shared, UserRole.ADMIN, password="SecondPass1!")

        user, _ = await auth_service.authenticate(
            session,
            LoginRequest(email=shared, password="SecondPass1!", business_slug=other_business.slug),
        )
        assert user.business_id == other_business.id

        # Without the slug, the password alone still picks the right account.
        first, _ = await auth_service.authenticate(
            session, LoginRequest(email=shared, password="FirstPass1!")
        )
        assert first.business_id == business.id

    async def test_slug_scopes_the_lookup(
        self, session: AsyncSession, business: Business, other_business: Business
    ):
        stranger = await make_user(session, other_business, f"stranger@{DOMAIN}", UserRole.OWNER)

        # The password is right, but for a user in a different tenant.
        with pytest.raises(AuthenticationError):
            await auth_service.authenticate(
                session,
                LoginRequest(
                    email=stranger.email,
                    password=FIXTURE_PASSWORD,
                    business_slug=business.slug,
                ),
            )

    async def test_login_endpoint_round_trip(self, client: AsyncClient, account: User):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": account.email, "password": FIXTURE_PASSWORD},
        )
        assert response.status_code == 200
        assert response.json()["token_type"] == "bearer"

    async def test_login_endpoint_rejects_bad_credentials_with_401(
        self, client: AsyncClient, account: User
    ):
        response = await client.post(
            "/api/v1/auth/login", json={"email": account.email, "password": "wrong-one"}
        )
        assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Brute-force throttling
# --------------------------------------------------------------------------- #
class TestLoginThrottling:
    """``test_throttle`` covers the counters; this covers the wiring into login.

    What matters here is that the budget is spent by *failures only*, that a
    correct password both succeeds and resets the count, and that a locked
    account stays locked even when the right password finally arrives.
    """

    async def test_repeated_failures_eventually_lock_the_account(
        self, session: AsyncSession, account: User
    ):
        for _ in range(MAX_ATTEMPTS_PER_ACCOUNT):
            with pytest.raises(AuthenticationError):
                await auth_service.authenticate(
                    session, LoginRequest(email=account.email, password="wrong-one")
                )

        with pytest.raises(LoginThrottleError):
            await auth_service.authenticate(
                session, LoginRequest(email=account.email, password="wrong-one")
            )

    async def test_the_correct_password_is_refused_once_locked(
        self, session: AsyncSession, account: User
    ):
        """The lockout is the whole point — it must outrank a valid credential."""
        for _ in range(MAX_ATTEMPTS_PER_ACCOUNT):
            with pytest.raises(AuthenticationError):
                await auth_service.authenticate(
                    session, LoginRequest(email=account.email, password="wrong-one")
                )

        with pytest.raises(LoginThrottleError):
            await auth_service.authenticate(
                session, LoginRequest(email=account.email, password=FIXTURE_PASSWORD)
            )

    async def test_successful_sign_ins_never_consume_the_budget(
        self, session: AsyncSession, account: User
    ):
        for _ in range(MAX_ATTEMPTS_PER_ACCOUNT + 5):
            await auth_service.authenticate(
                session, LoginRequest(email=account.email, password=FIXTURE_PASSWORD)
            )

    async def test_a_success_resets_the_count(self, session: AsyncSession, account: User):
        """Two typos then the right password must not leave the account primed."""
        for _ in range(MAX_ATTEMPTS_PER_ACCOUNT - 1):
            with pytest.raises(AuthenticationError):
                await auth_service.authenticate(
                    session, LoginRequest(email=account.email, password="wrong-one")
                )

        await auth_service.authenticate(
            session, LoginRequest(email=account.email, password=FIXTURE_PASSWORD)
        )

        # The budget is fresh, so another near-full run of failures is possible.
        for _ in range(MAX_ATTEMPTS_PER_ACCOUNT - 1):
            with pytest.raises(AuthenticationError):
                await auth_service.authenticate(
                    session, LoginRequest(email=account.email, password="wrong-one")
                )

    async def test_guesses_against_an_unknown_email_are_also_counted(
        self, session: AsyncSession
    ):
        """Otherwise an attacker enumerates freely as long as they miss."""
        unknown = f"ghost@{DOMAIN}"
        for _ in range(MAX_ATTEMPTS_PER_ACCOUNT):
            with pytest.raises(AuthenticationError):
                await auth_service.authenticate(
                    session, LoginRequest(email=unknown, password="wrong-one")
                )

        with pytest.raises(LoginThrottleError):
            await auth_service.authenticate(
                session, LoginRequest(email=unknown, password="wrong-one")
            )

    async def test_one_locked_account_does_not_lock_another(
        self, session: AsyncSession, account: User, business: Business
    ):
        for _ in range(MAX_ATTEMPTS_PER_ACCOUNT):
            with pytest.raises(AuthenticationError):
                await auth_service.authenticate(
                    session, LoginRequest(email=account.email, password="wrong-one")
                )

        colleague = await auth_service.create_user(
            session,
            business.id,
            UserCreate(
                email=f"colleague@{DOMAIN}",
                full_name="Ravi Kumar",
                password=FIXTURE_PASSWORD,
                role=UserRole.VIEWER,
            ),
        )
        user, _ = await auth_service.authenticate(
            session, LoginRequest(email=colleague.email, password=FIXTURE_PASSWORD)
        )
        assert user.id == colleague.id

    async def test_the_endpoint_answers_429_once_locked(
        self, client: AsyncClient, account: User
    ):
        for _ in range(MAX_ATTEMPTS_PER_ACCOUNT):
            await client.post(
                "/api/v1/auth/login",
                json={"email": account.email, "password": "wrong-one"},
            )

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": account.email, "password": FIXTURE_PASSWORD},
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "too_many_attempts"

    async def test_the_lockout_lifts_when_the_window_passes(
        self, client: AsyncClient, account: User, fake_redis
    ):
        for _ in range(MAX_ATTEMPTS_PER_ACCOUNT):
            await client.post(
                "/api/v1/auth/login",
                json={"email": account.email, "password": "wrong-one"},
            )
        fake_redis.advance(WINDOW_SECONDS + 1)

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": account.email, "password": FIXTURE_PASSWORD},
        )
        assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Refresh rotation
# --------------------------------------------------------------------------- #
class TestRefreshRotation:
    async def test_issue_records_a_digest_not_the_token(self, session: AsyncSession, owner: User):
        tokens = await auth_service.issue_token_pair(session, owner)
        rows = await refresh_rows(session, owner.id)

        assert len(rows) == 1
        assert rows[0].token_hash == hash_token(tokens.refresh_token)
        assert tokens.refresh_token not in rows[0].token_hash

    async def test_user_agent_is_recorded_and_truncated(self, session: AsyncSession, owner: User):
        await auth_service.issue_token_pair(session, owner, user_agent="C" * 500)
        rows = await refresh_rows(session, owner.id)

        assert rows[0].user_agent is not None
        assert len(rows[0].user_agent) == 300

    async def test_blank_user_agent_is_stored_as_null(self, session: AsyncSession, owner: User):
        await auth_service.issue_token_pair(session, owner, user_agent="")
        rows = await refresh_rows(session, owner.id)
        assert rows[0].user_agent is None

    async def test_refresh_rotates_and_revokes_the_presented_token(
        self, session: AsyncSession, owner: User
    ):
        first = await auth_service.issue_token_pair(session, owner)
        second = await auth_service.refresh_tokens(session, first.refresh_token)

        assert second.refresh_token != first.refresh_token
        rows = {row.token_hash: row for row in await refresh_rows(session, owner.id)}
        assert rows[hash_token(first.refresh_token)].revoked_at is not None
        assert rows[hash_token(second.refresh_token)].revoked_at is None

    async def test_replaying_a_rotated_token_revokes_the_whole_family(
        self, session: AsyncSession, owner: User
    ):
        """The signature of a stolen token: the same one presented twice.

        Whoever replays it is either the thief or the victim, and the server
        cannot tell which — so every token in the family is burned and both
        parties have to sign in again.
        """
        first = await auth_service.issue_token_pair(session, owner)
        second = await auth_service.refresh_tokens(session, first.refresh_token)

        with pytest.raises(AuthenticationError, match="already been used"):
            await auth_service.refresh_tokens(session, first.refresh_token)

        # The token minted from the replayed one is dead too.
        assert all(row.revoked_at is not None for row in await refresh_rows(session, owner.id))
        with pytest.raises(AuthenticationError):
            await auth_service.refresh_tokens(session, second.refresh_token)

    async def test_unknown_refresh_token_is_rejected(self, session: AsyncSession, owner: User):
        # Correctly signed, but never issued by this server.
        orphan, _ = create_refresh_token(
            user_id=owner.id, business_id=owner.business_id, role=owner.role
        )
        with pytest.raises(AuthenticationError, match="not recognised"):
            await auth_service.refresh_tokens(session, orphan)

    async def test_expired_record_is_rejected(self, session: AsyncSession, owner: User):
        tokens = await auth_service.issue_token_pair(session, owner)
        rows = await refresh_rows(session, owner.id)
        rows[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.flush()

        with pytest.raises(AuthenticationError, match="expired"):
            await auth_service.refresh_tokens(session, tokens.refresh_token)

    async def test_access_token_cannot_be_used_to_refresh(self, session: AsyncSession, owner: User):
        tokens = await auth_service.issue_token_pair(session, owner)
        with pytest.raises(AuthenticationError):
            await auth_service.refresh_tokens(session, tokens.access_token)

    async def test_deactivated_user_cannot_refresh(self, session: AsyncSession, owner: User):
        tokens = await auth_service.issue_token_pair(session, owner)
        owner.is_active = False
        await session.flush()

        with pytest.raises(AuthenticationError, match="no longer active"):
            await auth_service.refresh_tokens(session, tokens.refresh_token)

    async def test_revoke_is_idempotent(self, session: AsyncSession, owner: User):
        tokens = await auth_service.issue_token_pair(session, owner)
        await auth_service.revoke_refresh_token(session, tokens.refresh_token)
        first_revoked_at = (await refresh_rows(session, owner.id))[0].revoked_at

        await auth_service.revoke_refresh_token(session, tokens.refresh_token)
        assert (await refresh_rows(session, owner.id))[0].revoked_at == first_revoked_at

    async def test_revoking_an_unknown_token_is_silent(self, session: AsyncSession, owner: User):
        # Logout must not become a probe for which tokens exist.
        await auth_service.revoke_refresh_token(session, "never-issued")
        assert await refresh_rows(session, owner.id) == []

    async def test_revoke_all_counts_only_live_tokens(self, session: AsyncSession, owner: User):
        first = await auth_service.issue_token_pair(session, owner)
        await auth_service.issue_token_pair(session, owner)
        await auth_service.revoke_refresh_token(session, first.refresh_token)

        assert await auth_service.revoke_all_for_user(session, owner.id) == 1
        assert await auth_service.revoke_all_for_user(session, owner.id) == 0

    async def test_refresh_endpoint_round_trip(self, client: AsyncClient, account: User):
        login = await client.post(
            "/api/v1/auth/login", json={"email": account.email, "password": FIXTURE_PASSWORD}
        )
        refresh_token = login.json()["refresh_token"]

        rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert rotated.status_code == 200
        assert rotated.json()["refresh_token"] != refresh_token

        replayed = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert replayed.status_code == 401

    async def test_logout_endpoint_kills_the_token(self, client: AsyncClient, account: User):
        login = await client.post(
            "/api/v1/auth/login", json={"email": account.email, "password": FIXTURE_PASSWORD}
        )
        refresh_token = login.json()["refresh_token"]

        signed_out = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
        assert signed_out.status_code == 200

        reused = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert reused.status_code == 401


# --------------------------------------------------------------------------- #
# Password change
# --------------------------------------------------------------------------- #
class TestPasswordChange:
    async def test_change_sets_a_new_hash(self, session: AsyncSession, account: User):
        before = account.password_hash
        await auth_service.change_password(session, account, FIXTURE_PASSWORD, "Br4ndNewPass!")

        assert account.password_hash != before
        user, _ = await auth_service.authenticate(
            session, LoginRequest(email=account.email, password="Br4ndNewPass!")
        )
        assert user.id == account.id

    async def test_wrong_current_password_is_rejected(self, session: AsyncSession, owner: User):
        with pytest.raises(AuthenticationError, match="incorrect"):
            await auth_service.change_password(session, owner, "not-it", "Br4ndNewPass!")

    async def test_reusing_the_same_password_is_rejected(self, session: AsyncSession, owner: User):
        with pytest.raises(ValidationError, match="differ"):
            await auth_service.change_password(session, owner, FIXTURE_PASSWORD, FIXTURE_PASSWORD)

    async def test_change_signs_out_every_other_session(self, session: AsyncSession, owner: User):
        laptop = await auth_service.issue_token_pair(session, owner)
        phone = await auth_service.issue_token_pair(session, owner)

        await auth_service.change_password(session, owner, FIXTURE_PASSWORD, "Br4ndNewPass!")

        for stale in (laptop, phone):
            with pytest.raises(AuthenticationError):
                await auth_service.refresh_tokens(session, stale.refresh_token)

    async def test_change_password_endpoint(self, client: AsyncClient, owner_headers: dict):
        response = await client.post(
            "/api/v1/auth/change-password",
            headers=owner_headers,
            json={"current_password": FIXTURE_PASSWORD, "new_password": "Br4ndNewPass!"},
        )
        assert response.status_code == 200

    async def test_change_password_endpoint_rejects_a_weak_new_password(
        self, client: AsyncClient, owner_headers: dict
    ):
        response = await client.post(
            "/api/v1/auth/change-password",
            headers=owner_headers,
            json={"current_password": FIXTURE_PASSWORD, "new_password": "short1!"},
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Team management
# --------------------------------------------------------------------------- #
class TestTeam:
    async def test_admin_creates_a_colleague(self, session: AsyncSession, business: Business):
        user = await auth_service.create_user(
            session,
            business.id,
            UserCreate(
                email=f"Front.Desk@{DOMAIN}",
                full_name="Front Desk",
                password="FrontDesk1!",
                role=UserRole.SUPERVISOR,
                phone="9876500042",
            ),
        )

        assert user.email == f"front.desk@{DOMAIN}"
        assert user.business_id == business.id
        assert user.role == UserRole.SUPERVISOR
        assert user.phone == "+919876500042"

    async def test_duplicate_email_within_a_tenant_conflicts(
        self, session: AsyncSession, business: Business, account: User
    ):
        with pytest.raises(ConflictError):
            await auth_service.create_user(
                session,
                business.id,
                UserCreate(email=account.email, full_name="Impostor", password="Impostor1!"),
            )

    async def test_the_same_email_may_exist_in_another_tenant(
        self, session: AsyncSession, other_business: Business, account: User
    ):
        # Tenants are separate companies; one address working at both is a
        # normal arrangement, not a collision.
        user = await auth_service.create_user(
            session,
            other_business.id,
            UserCreate(email=account.email, full_name="Also Here", password="AlsoHere1!"),
        )
        assert user.business_id == other_business.id

    async def test_update_changes_only_what_was_sent(
        self, session: AsyncSession, business: Business, viewer: User
    ):
        original_name = viewer.full_name
        updated = await auth_service.update_user(
            session, business.id, viewer.id, UserUpdate(role=UserRole.ADMIN)
        )

        assert updated.role == UserRole.ADMIN
        assert updated.full_name == original_name

    async def test_update_of_a_missing_user_is_a_404(
        self, session: AsyncSession, business: Business
    ):
        with pytest.raises(NotFoundError):
            await auth_service.update_user(
                session, business.id, uuid.uuid4(), UserUpdate(full_name="Ghost")
            )

    async def test_a_user_from_another_tenant_is_invisible(
        self, session: AsyncSession, business: Business, other_owner: User
    ):
        with pytest.raises(NotFoundError):
            await auth_service.update_user(
                session, business.id, other_owner.id, UserUpdate(is_active=False)
            )

    async def test_the_last_owner_cannot_be_demoted(
        self, session: AsyncSession, business: Business, owner: User
    ):
        with pytest.raises(ValidationError, match="at least one active owner"):
            await auth_service.update_user(
                session, business.id, owner.id, UserUpdate(role=UserRole.VIEWER)
            )

    async def test_the_last_owner_cannot_be_deactivated(
        self, session: AsyncSession, business: Business, owner: User
    ):
        with pytest.raises(ValidationError):
            await auth_service.update_user(
                session, business.id, owner.id, UserUpdate(is_active=False)
            )

    async def test_the_last_owner_cannot_be_deleted(
        self, session: AsyncSession, business: Business, owner: User
    ):
        with pytest.raises(ValidationError):
            await auth_service.soft_delete_user(session, business.id, owner.id)

    async def test_an_owner_can_step_down_once_another_exists(
        self, session: AsyncSession, business: Business, owner: User
    ):
        successor = await auth_service.create_user(
            session,
            business.id,
            UserCreate(
                email=f"successor@{DOMAIN}",
                full_name="Second Owner",
                password="Successor1!",
                role=UserRole.OWNER,
            ),
        )
        assert successor.role == UserRole.OWNER

        demoted = await auth_service.update_user(
            session, business.id, owner.id, UserUpdate(role=UserRole.ADMIN)
        )
        assert demoted.role == UserRole.ADMIN

    async def test_an_inactive_owner_does_not_count_as_cover(
        self, session: AsyncSession, business: Business, owner: User
    ):
        """A deactivated owner cannot let anyone in, so they are not a spare."""
        dormant = await make_user(session, business, f"dormant@{DOMAIN}", UserRole.OWNER)
        dormant.is_active = False
        await session.flush()

        with pytest.raises(ValidationError):
            await auth_service.soft_delete_user(session, business.id, owner.id)

    async def test_deleting_a_user_signs_them_out_everywhere(
        self, session: AsyncSession, business: Business, viewer: User
    ):
        tokens = await auth_service.issue_token_pair(session, viewer)
        await auth_service.soft_delete_user(session, business.id, viewer.id)

        assert viewer.deleted_at is not None
        assert viewer.is_active is False
        with pytest.raises(AuthenticationError):
            await auth_service.refresh_tokens(session, tokens.refresh_token)

    async def test_a_deleted_user_cannot_sign_back_in(
        self, session: AsyncSession, business: Business, colleague: User
    ):
        await auth_service.soft_delete_user(session, business.id, colleague.id)

        with pytest.raises(AuthenticationError):
            await auth_service.authenticate(
                session, LoginRequest(email=colleague.email, password=FIXTURE_PASSWORD)
            )

    async def test_a_deleted_address_can_be_re_invited(
        self, session: AsyncSession, business: Business, colleague: User
    ):
        # Someone leaves and rejoins. Their old row is kept for audit, so the
        # uniqueness check has to look past soft-deleted rows — otherwise the
        # address is burned forever.
        await auth_service.soft_delete_user(session, business.id, colleague.id)

        rehired = await auth_service.create_user(
            session,
            business.id,
            UserCreate(
                email=colleague.email, full_name="Returning Colleague", password="Returning1!"
            ),
        )
        assert rehired.id != colleague.id
        assert rehired.is_active

    async def test_a_deleted_user_is_not_counted_as_an_owner(
        self, session: AsyncSession, business: Business, owner: User
    ):
        """Deleting one of two owners must not leave the tenant ownerless."""
        second = await make_user(session, business, f"second.owner@{DOMAIN}", UserRole.OWNER)
        await auth_service.soft_delete_user(session, business.id, second.id)

        with pytest.raises(ValidationError):
            await auth_service.soft_delete_user(session, business.id, owner.id)


# --------------------------------------------------------------------------- #
# Role gates on the HTTP surface
# --------------------------------------------------------------------------- #
class TestTeamEndpoints:
    async def test_owner_lists_the_team(
        self, client: AsyncClient, owner_headers: dict, viewer: User
    ):
        response = await client.get("/api/v1/auth/users", headers=owner_headers)
        assert response.status_code == 200
        assert viewer.email in {row["email"] for row in response.json()}

    async def test_the_list_stops_at_the_tenant_boundary(
        self, client: AsyncClient, owner_headers: dict, other_owner: User
    ):
        response = await client.get("/api/v1/auth/users", headers=owner_headers)
        assert other_owner.email not in {row["email"] for row in response.json()}

    async def test_a_viewer_cannot_invite(self, client: AsyncClient, viewer_headers: dict):
        response = await client.post(
            "/api/v1/auth/users",
            headers=viewer_headers,
            json={
                "email": f"smuggled@{DOMAIN}",
                "full_name": "Smuggled In",
                "password": "Smuggled1!",
                "role": "owner",
            },
        )
        assert response.status_code == 403

    async def test_a_supervisor_cannot_invite(self, client: AsyncClient, supervisor_headers: dict):
        response = await client.post(
            "/api/v1/auth/users",
            headers=supervisor_headers,
            json={
                "email": f"smuggled@{DOMAIN}",
                "full_name": "Smuggled In",
                "password": "Smuggled1!",
            },
        )
        assert response.status_code == 403

    async def test_an_admin_can_invite(
        self, client: AsyncClient, session: AsyncSession, business: Business
    ):
        admin = await make_user(session, business, f"admin@{DOMAIN}", UserRole.ADMIN)

        response = await client.post(
            "/api/v1/auth/users",
            headers=auth_headers(admin),
            json={
                "email": f"new.hire@{DOMAIN}",
                "full_name": "New Hire",
                "password": "NewHire01!",
                "role": "viewer",
            },
        )
        assert response.status_code == 201
        assert response.json()["role"] == "viewer"

    async def test_an_invite_never_echoes_the_password(
        self, client: AsyncClient, owner_headers: dict
    ):
        response = await client.post(
            "/api/v1/auth/users",
            headers=owner_headers,
            json={
                "email": f"new.hire@{DOMAIN}",
                "full_name": "New Hire",
                "password": "NewHire01!",
            },
        )
        assert response.status_code == 201
        assert "NewHire01!" not in response.text

    async def test_deleting_a_user_over_http(
        self, client: AsyncClient, owner_headers: dict, viewer: User
    ):
        response = await client.delete(f"/api/v1/auth/users/{viewer.id}", headers=owner_headers)
        assert response.status_code == 204

        listing = await client.get("/api/v1/auth/users", headers=owner_headers)
        assert viewer.email not in {row["email"] for row in listing.json()}

    async def test_deleting_across_tenants_is_a_404(
        self, client: AsyncClient, owner_headers: dict, other_owner: User
    ):
        response = await client.delete(
            f"/api/v1/auth/users/{other_owner.id}", headers=owner_headers
        )
        assert response.status_code == 404

    async def test_last_owner_deletion_is_a_422_over_http(
        self, client: AsyncClient, owner_headers: dict, owner: User
    ):
        response = await client.delete(f"/api/v1/auth/users/{owner.id}", headers=owner_headers)
        assert response.status_code == 422

    async def test_business_profile_is_readable_by_a_viewer(
        self, client: AsyncClient, viewer_headers: dict, business: Business
    ):
        response = await client.get("/api/v1/auth/business", headers=viewer_headers)
        assert response.status_code == 200
        assert response.json()["slug"] == business.slug

    async def test_only_an_admin_may_edit_the_business(
        self, client: AsyncClient, viewer_headers: dict, owner_headers: dict
    ):
        blocked = await client.patch(
            "/api/v1/auth/business", headers=viewer_headers, json={"city": "Nagpur"}
        )
        assert blocked.status_code == 403

        allowed = await client.patch(
            "/api/v1/auth/business", headers=owner_headers, json={"city": "Nagpur"}
        )
        assert allowed.status_code == 200
        assert allowed.json()["city"] == "Nagpur"

    async def test_every_editable_field_is_readable_back(
        self, client: AsyncClient, owner_headers: dict
    ):
        # A settings form round-trips what it saved. A field the API accepts but
        # never returns silently blanks itself the next time the form loads.
        payload = {
            "name": "Sunrise Diagnostics LLP",
            "industry": "diagnostics",
            "gstin": "27AAAAA0000A1Z5",
            "address": "3rd floor, Kalyani Nagar",
            "city": "Pune",
            "state": "Maharashtra",
        }
        response = await client.patch(
            "/api/v1/auth/business", headers=owner_headers, json=payload
        )

        assert response.status_code == 200
        assert {key: response.json()[key] for key in payload} == payload

    async def test_business_update_normalises_the_phone(
        self, client: AsyncClient, owner_headers: dict
    ):
        response = await client.patch(
            "/api/v1/auth/business", headers=owner_headers, json={"phone": "98765 00099"}
        )
        assert response.json()["phone"] == "+919876500099"

    async def test_business_update_cannot_change_the_plan(
        self, client: AsyncClient, owner_headers: dict
    ):
        # Plan changes go through billing, which prices them. Accepting one
        # here would hand every admin a free upgrade.
        response = await client.patch(
            "/api/v1/auth/business",
            headers=owner_headers,
            json={"plan": PlanTier.ENTERPRISE.value},
        )
        assert response.status_code == 200
        assert response.json()["plan"] == PlanTier.GROWTH.value

    async def test_an_unsigned_request_is_a_401(self, client: AsyncClient):
        assert (await client.get("/api/v1/auth/me")).status_code == 401

    async def test_a_token_for_a_deleted_user_stops_working(
        self, client: AsyncClient, session: AsyncSession, business: Business, viewer: User
    ):
        headers = auth_headers(viewer)
        assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200

        await auth_service.soft_delete_user(session, business.id, viewer.id)
        assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401

    async def test_a_token_signed_for_a_missing_user_is_rejected(self, client: AsyncClient):
        token, _ = create_access_token(
            user_id=uuid.uuid4(), business_id=uuid.uuid4(), role=UserRole.OWNER
        )
        response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Slug allocation
# --------------------------------------------------------------------------- #
class TestSlugAllocation:
    async def test_repeated_names_each_get_their_own_slug(self, session: AsyncSession):
        slugs = set()
        for index in range(4):
            business, _, _ = await auth_service.register_business(
                session, register_payload(email=f"owner{index}@lotusdental.in")
            )
            slugs.add(business.slug)

        assert len(slugs) == 4
        total = await session.scalar(select(func.count()).select_from(Business))
        assert total == 4

"""
Single-tenant data model. This backend is deployed once per customer -
each company gets its own instance (its own database, its own
credentials), and a second customer gets an entirely separate deployment
of the same codebase, not a new row in a shared table. That's a deliberate
choice, not an oversight: it trades the lower per-customer marginal cost
of a shared multi-tenant backend for the much simpler fact that one
customer's bug or breach can't touch another's - there's no cross-customer
isolation to get right in the first place, because there's no
cross-customer anything.

Config is the generic replacement for what an earlier one-off desktop tool
hardcoded directly in a script: the same categories of value (site->OU,
company->domain, phone fallback, group names, license SKUs), but stored as
data an admin edits through the API rather than a maintainer editing a
script. It's a singleton - always the row with id=1 - since a
single-tenant deployment only ever needs one.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel, Column, JSON

SINGLETON_ID = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CloudGroupRule(SQLModel):
    """One Entra group this deployment wants applied to matching joiners.

    `condition` is deliberately just a small set of well-known trigger
    keys rather than free-text logic, so an admin fills this in through a
    form instead of writing code:
      - "always"                        -> every joiner gets this group
      - "is_line_manager"               -> only if the form flags them as a manager
      - "has_mobile_device"             -> only if the form requests a mobile device
      - "field:<FieldName>"             -> only if that named form field is filled
                                            in (and isn't placeholder text)
      - "field_matches:<Field>:<Regex>" -> only if that field's value matches
                                            the regex (e.g. picking a "tech"
                                            VPN/CA group by site or department)
      - "field_not_matches:<Field>:<Regex>" -> the inverse - pairs with the
                                            rule above to express "this group
                                            OR that group" mutually exclusive
                                            choices as two independent rules
    """
    key: str
    display_name: str
    condition: str = "always"


class Config(SQLModel, table=True):
    id: int = Field(default=SINGLETON_ID, primary_key=True)

    # Site -> on-prem OU distinguished name, used only when the "similar
    # colleague" account can't be found in AD.
    site_ou_fallback: dict = Field(default_factory=dict, sa_column=Column(JSON))

    # Company name (as it appears on the joiner form) -> UPN/email domain,
    # used only when the colleague's own UPN domain can't be read.
    company_domain_fallback: dict = Field(default_factory=dict, sa_column=Column(JSON))

    # Site -> fallback office phone number.
    site_phone_fallback: dict = Field(default_factory=dict, sa_column=Column(JSON))

    # Site -> ISO 3166-1 alpha-2 country code, used to derive Graph
    # UsageLocation when the colleague's AD Country attribute is missing
    # or malformed. Replaces the country-specific hardcoded site list an earlier version of this tool used.
    site_country_code: dict = Field(default_factory=dict, sa_column=Column(JSON))
    default_country_code: str = "GB"

    # Wildcard patterns for on-prem groups that should never be cloned from
    # a similar colleague (e.g. one-off award/survey distribution lists).
    excluded_group_patterns: list = Field(default_factory=list, sa_column=Column(JSON))

    # Name of the on-prem group added when the form flags someone as a
    # line manager.
    line_manager_group: Optional[str] = None

    # Entra groups to evaluate for every joiner. Stored as a list of
    # CloudGroupRule dicts. Kept as a bare `list` here (not
    # list[CloudGroupRule]) because SQLModel doesn't run full nested-model
    # validation through a JSON column on a table=True model - annotating it
    # as list[CloudGroupRule] looks like it validates but silently doesn't,
    # so routes/admin.py validates each rule explicitly instead (see
    # put_config).
    cloud_group_rules: list = Field(default_factory=list, sa_column=Column(JSON))

    # Base M365 license SKU part number, plus an optional second SKU only
    # added when the form requests a mobile device.
    primary_license_sku: Optional[str] = None
    mobile_license_sku: Optional[str] = None
    default_license_sku: Optional[str] = None

    # Column/row map for the joiner Excel template, e.g.
    # {"full_name": "E6", "similar_colleague": "H7", ...}.
    excel_field_map: dict = Field(default_factory=dict, sa_column=Column(JSON))

    # Hostname of an on-prem server the connector should trigger a delta
    # AD Connect sync on after creating the user (optional).
    ad_sync_server: Optional[str] = None

    # No graph_scopes field: app-only client-credentials auth (see
    # graph_actions.py) always requests scope "https://graph.microsoft.com/
    # .default" and gets back whatever application permissions were
    # statically granted to the Entra app registration itself - there's no
    # per-request scope list to configure here. The actual required
    # permissions (User.ReadWrite.All, Group.ReadWrite.All,
    # Organization.Read.All - NOT Directory.ReadWrite.All, which nothing
    # here needs) are documented in DEPLOY_AZURE.md's app registration step.

    updated_at: datetime = Field(default_factory=utcnow)


class JobStatus(str, Enum):
    pending = "pending"
    claimed = "claimed"
    running = "running"
    awaiting_cloud_steps = "awaiting_cloud_steps"
    succeeded = "succeeded"
    failed = "failed"


class Job(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    status: JobStatus = Field(default=JobStatus.pending, index=True)

    # Structured joiner-form fields, parsed by the client upload step using
    # excel_field_map, not by the connector.
    joiner_fields: dict = Field(default_factory=dict, sa_column=Column(JSON))

    ad_samaccountname: Optional[str] = None
    ad_upn: Optional[str] = None

    log_lines: list = Field(default_factory=list, sa_column=Column(JSON))
    error_message: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    claimed_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utcnow)

    # Set atomically by _try_claim_for_cloud_steps() before run_cloud_steps
    # does any work, and re-checked (with a staleness window) by
    # find_interrupted_jobs() - this is what stops two backend instances
    # from both processing the same job's Graph steps if this is ever
    # scaled beyond the single-instance assumption the rest of this
    # project makes.
    cloud_steps_claimed_at: Optional[datetime] = None


class GraphCredential(SQLModel, table=True):
    """The Entra app registration used for the Graph-only steps (group
    membership, licensing) the backend runs directly. Singleton, like
    Config - one deployment, one set of Graph credentials.

    client_secret's real value never has to live here: routes/admin.py
    writes it through secrets_backend.store_secret(), which puts the actual
    secret in Azure Key Vault when AZURE_KEY_VAULT_URL is set and stores
    only its Key Vault secret name in this column. graph_actions.py reads
    it back through resolve_secret() the same way. Without Key Vault
    configured (local dev only), both functions are no-ops and this column
    holds the raw value directly - fine for a laptop, not for a real
    deployment.
    """
    id: int = Field(default=SINGLETON_ID, primary_key=True)
    aad_tenant_id: str
    client_id: str
    client_secret: str


class KeyRole(str, Enum):
    # On-prem agent: claims/executes jobs, reads config. Cannot create
    # jobs or manage config - it only does what the backend hands it.
    connector = "connector"
    # Admin-team GUI/script: creates jobs (e.g. via Excel upload) and reads
    # job status. Cannot claim/execute jobs or manage config - kept
    # separate from both the connector and admin keys so a compromised
    # laptop running the GUI can't do AD writes or reconfigure OUs/groups.
    client = "client"
    # A named person with operator access: can edit Config, manage the
    # Graph credential, issue/revoke keys. Separate from the single
    # DASHBOARD_TOKEN "root" credential (see auth.py) so more than one
    # person can have their own individually revocable access, and the
    # audit log can say which person did something instead of just
    # "dashboard" for everyone.
    admin = "admin"


class AuditLog(SQLModel, table=True):
    """Append-only trail of who changed what. "actor" identifies which
    credential acted - "root" for the DASHBOARD_TOKEN, "admin:<label>" for
    a named admin key, or "connector:<label>" / "client:<label>" for an
    issued key - good enough to answer "when was config last changed" and
    "who issued/revoked this key" without trusting anyone's memory of it.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    actor: str
    action: str
    detail: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ApiKey(SQLModel, table=True):
    """API key scoped to one role. Only the hash is stored; the plaintext
    key is shown once at creation time, the same "shown once, never
    persisted" pattern the connector uses for temporary passwords.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    role: KeyRole = Field(default=KeyRole.connector)
    label: str
    key_hash: str
    created_at: datetime = Field(default_factory=utcnow)
    revoked: bool = False

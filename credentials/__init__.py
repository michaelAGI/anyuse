"""Credential management module."""
from anyuse.credentials.vault import CredentialVault
from anyuse.credentials.accounts import (
    AccountMetadata,
    AccountRegistry,
    CredentialMode,
    CredentialType,
    create_account,
)
from anyuse.credentials.sso import (
    SSOProvider,
    SSOConfig,
    SSOManager,
    create_sso_config,
)
from anyuse.credentials.two_factor import (
    TwoFactorMethod,
    TwoFactorConfig,
    TwoFactorHandler,
    TOTPGenerator,
    SMSCodeReader,
    PushApprovalWaiter,
    BackupCodeManager,
)
from anyuse.credentials.generator import (
    CredentialGenerator,
    generate_secure_password,
    generate_passphrase,
    generate_username,
    generate_email,
    generate_api_key,
    generate_recovery_codes,
    password_strength,
)

__all__ = [
    # Vault
    "CredentialVault",
    # Accounts
    "AccountMetadata",
    "AccountRegistry",
    "CredentialMode",
    "CredentialType",
    "create_account",
    # SSO
    "SSOProvider",
    "SSOConfig",
    "SSOManager",
    "create_sso_config",
    # Two-Factor
    "TwoFactorMethod",
    "TwoFactorConfig",
    "TwoFactorHandler",
    "TOTPGenerator",
    "SMSCodeReader",
    "PushApprovalWaiter",
    "BackupCodeManager",
    # Generator
    "CredentialGenerator",
    "generate_secure_password",
    "generate_passphrase",
    "generate_username",
    "generate_email",
    "generate_api_key",
    "generate_recovery_codes",
    "password_strength",
]

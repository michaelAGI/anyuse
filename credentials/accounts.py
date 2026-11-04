"""
Account Registry - Track accounts with full metadata.

Stores information about:
- Websites
- Desktop applications
- CLI tools
- API keys
- System services

Including SSO and 2FA information.
"""
import json
from pathlib import Path
from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, List, Dict, Any

from .sso import SSOProvider
from .two_factor import TwoFactorMethod, TwoFactorConfig
from anyuse.paths import anyuse_data_path


class CredentialType(Enum):
    """Types of credentials/accounts."""
    WEBSITE = "website"     # Web accounts (github.com, twitter.com)
    APP = "app"             # Desktop apps (Slack, Discord, Spotify)
    CLI = "cli"             # CLI tools (git, npm, docker)
    API = "api"             # API keys (OpenAI, AWS)
    SYSTEM = "system"       # System services (SSH, VPN, WiFi)
    OTHER = "other"         # Anything else


class CredentialMode(Enum):
    """Credential storage modes."""
    USER = "user"       # User's existing credentials (no prefix)
    AGENT = "agent"     # Agent-created accounts (prefixed)


@dataclass
class AccountMetadata:
    """
    Full metadata for an account/credential.
    
    Supports websites, apps, CLI tools, APIs, and system services.
    Includes SSO and 2FA information.
    """
    # Identity
    name: str                              # Human-readable name
    credential_type: CredentialType        # Type of credential
    service: str                           # Service identifier (domain/app name)
    
    # Location - where credential is used
    url: Optional[str] = None              # URL for websites
    app_bundle_id: Optional[str] = None    # macOS bundle ID for apps
    cli_command: Optional[str] = None      # Command for CLI tools
    
    # Account details
    username: str = ""
    email: Optional[str] = None
    password_alias: Optional[str] = None   # Keychain alias for password
    api_key_alias: Optional[str] = None    # Keychain alias for API key
    
    # SSO information
    created_via_sso: bool = False
    sso_provider: Optional[SSOProvider] = None
    sso_account: Optional[str] = None      # Which SSO account was used
    
    # 2FA information
    has_2fa: bool = False
    two_factor_method: Optional[TwoFactorMethod] = None
    totp_secret_alias: Optional[str] = None
    backup_codes_alias: Optional[str] = None
    sms_sender: Optional[str] = None       # Expected SMS sender
    
    # Metadata
    created_at: str = ""                   # ISO timestamp
    updated_at: str = ""                   # ISO timestamp
    last_used: Optional[str] = None        # Last successful use
    created_by: str = "user"               # "user" or "agent"
    notes: str = ""
    tags: List[str] = field(default_factory=list)
    
    # Statistics
    use_count: int = 0
    last_error: Optional[str] = None
    
    def __post_init__(self):
        """Set timestamps if not provided."""
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = {
            "name": self.name,
            "credential_type": self.credential_type.value,
            "service": self.service,
            "url": self.url,
            "app_bundle_id": self.app_bundle_id,
            "cli_command": self.cli_command,
            "username": self.username,
            "email": self.email,
            "password_alias": self.password_alias,
            "api_key_alias": self.api_key_alias,
            "created_via_sso": self.created_via_sso,
            "sso_provider": self.sso_provider.value if self.sso_provider else None,
            "sso_account": self.sso_account,
            "has_2fa": self.has_2fa,
            "two_factor_method": self.two_factor_method.value if self.two_factor_method else None,
            "totp_secret_alias": self.totp_secret_alias,
            "backup_codes_alias": self.backup_codes_alias,
            "sms_sender": self.sms_sender,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used": self.last_used,
            "created_by": self.created_by,
            "notes": self.notes,
            "tags": self.tags,
            "use_count": self.use_count,
            "last_error": self.last_error,
        }
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AccountMetadata":
        """Create from dictionary."""
        return cls(
            name=data["name"],
            credential_type=CredentialType(data["credential_type"]),
            service=data["service"],
            url=data.get("url"),
            app_bundle_id=data.get("app_bundle_id"),
            cli_command=data.get("cli_command"),
            username=data.get("username", ""),
            email=data.get("email"),
            password_alias=data.get("password_alias"),
            api_key_alias=data.get("api_key_alias"),
            created_via_sso=data.get("created_via_sso", False),
            sso_provider=SSOProvider(data["sso_provider"]) if data.get("sso_provider") else None,
            sso_account=data.get("sso_account"),
            has_2fa=data.get("has_2fa", False),
            two_factor_method=TwoFactorMethod(data["two_factor_method"]) if data.get("two_factor_method") else None,
            totp_secret_alias=data.get("totp_secret_alias"),
            backup_codes_alias=data.get("backup_codes_alias"),
            sms_sender=data.get("sms_sender"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            last_used=data.get("last_used"),
            created_by=data.get("created_by", "user"),
            notes=data.get("notes", ""),
            tags=data.get("tags", []),
            use_count=data.get("use_count", 0),
            last_error=data.get("last_error"),
        )
    
    def update_last_used(self):
        """Update the last_used timestamp."""
        self.last_used = datetime.now().isoformat()
        self.updated_at = self.last_used
        self.use_count += 1
    
    def get_identifier(self) -> str:
        """Get unique identifier based on type."""
        if self.credential_type == CredentialType.WEBSITE:
            return self.url or self.service
        elif self.credential_type == CredentialType.APP:
            return self.app_bundle_id or self.service
        elif self.credential_type == CredentialType.CLI:
            return self.cli_command or self.service
        else:
            return self.service


class AccountRegistry:
    """
    Registry for storing and querying account metadata.
    
    Persists to JSON file at ~/.anyuse/accounts.json
    """
    
    DEFAULT_PATH = anyuse_data_path("accounts.json")
    
    def __init__(self, registry_path: Optional[Path] = None):
        """
        Initialize account registry.
        
        Args:
            registry_path: Path to registry file (uses default if None)
        """
        self.registry_path = registry_path or self.DEFAULT_PATH
        self._accounts: Dict[str, AccountMetadata] = {}
        self._load()
    
    def _load(self):
        """Load accounts from file."""
        if self.registry_path.exists():
            try:
                with open(self.registry_path) as f:
                    data = json.load(f)
                    for item in data.get("accounts", []):
                        account = AccountMetadata.from_dict(item)
                        self._accounts[account.service] = account
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Warning: Could not load accounts: {e}")
    
    def _save(self):
        """Save accounts to file."""
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "version": 1,
            "updated_at": datetime.now().isoformat(),
            "accounts": [acc.to_dict() for acc in self._accounts.values()],
        }
        
        with open(self.registry_path, 'w') as f:
            json.dump(data, f, indent=2)
    
    def register(self, account: AccountMetadata) -> None:
        """
        Register a new account or update existing.
        
        Args:
            account: Account metadata to store
        """
        account.updated_at = datetime.now().isoformat()
        self._accounts[account.service] = account
        self._save()
    
    def get(self, service: str) -> Optional[AccountMetadata]:
        """
        Get account by service name.
        
        Args:
            service: Service identifier (domain, app name, etc.)
        
        Returns:
            AccountMetadata or None
        """
        return self._accounts.get(service)
    
    def get_by_type(self, cred_type: CredentialType) -> List[AccountMetadata]:
        """
        Get all accounts of a specific type.
        
        Args:
            cred_type: Type to filter by
        
        Returns:
            List of matching accounts
        """
        return [
            acc for acc in self._accounts.values()
            if acc.credential_type == cred_type
        ]
    
    def get_by_bundle_id(self, bundle_id: str) -> Optional[AccountMetadata]:
        """
        Get account by macOS app bundle ID.
        
        Args:
            bundle_id: App bundle ID (e.g., "com.slack.Slack")
        
        Returns:
            AccountMetadata or None
        """
        for acc in self._accounts.values():
            if acc.app_bundle_id == bundle_id:
                return acc
        return None
    
    def get_by_creator(self, creator: str) -> List[AccountMetadata]:
        """
        Get accounts created by user or agent.
        
        Args:
            creator: "user" or "agent"
        
        Returns:
            List of matching accounts
        """
        return [
            acc for acc in self._accounts.values()
            if acc.created_by == creator
        ]
    
    def get_sso_accounts(self, provider: Optional[SSOProvider] = None) -> List[AccountMetadata]:
        """
        Get accounts created via SSO.
        
        Args:
            provider: Optional filter by SSO provider
        
        Returns:
            List of SSO accounts
        """
        accounts = [acc for acc in self._accounts.values() if acc.created_via_sso]
        
        if provider:
            accounts = [acc for acc in accounts if acc.sso_provider == provider]
        
        return accounts
    
    def find(self, query: str) -> List[AccountMetadata]:
        """
        Search accounts by query string.
        
        Searches in: name, service, username, email, notes, tags
        
        Args:
            query: Search query (case-insensitive)
        
        Returns:
            List of matching accounts
        """
        query_lower = query.lower()
        results = []
        
        for acc in self._accounts.values():
            searchable = " ".join([
                acc.name,
                acc.service,
                acc.username,
                acc.email or "",
                acc.notes,
                " ".join(acc.tags),
            ]).lower()
            
            if query_lower in searchable:
                results.append(acc)
        
        return results
    
    def has_account(self, service: str) -> bool:
        """Check if account exists for service."""
        return service in self._accounts
    
    def list_all(self) -> List[AccountMetadata]:
        """Get all accounts."""
        return list(self._accounts.values())
    
    def list_services(self) -> List[str]:
        """Get list of all service names."""
        return list(self._accounts.keys())
    
    def delete(self, service: str) -> bool:
        """
        Delete an account.
        
        Args:
            service: Service to delete
        
        Returns:
            True if deleted, False if not found
        """
        if service in self._accounts:
            del self._accounts[service]
            self._save()
            return True
        return False
    
    def update_last_used(self, service: str) -> bool:
        """
        Update the last_used timestamp for an account.
        
        Args:
            service: Service identifier
        
        Returns:
            True if updated, False if not found
        """
        if service in self._accounts:
            self._accounts[service].update_last_used()
            self._save()
            return True
        return False
    
    def set_error(self, service: str, error: str) -> bool:
        """
        Record an error for an account.
        
        Args:
            service: Service identifier
            error: Error message
        
        Returns:
            True if updated, False if not found
        """
        if service in self._accounts:
            self._accounts[service].last_error = error
            self._accounts[service].updated_at = datetime.now().isoformat()
            self._save()
            return True
        return False
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get registry statistics."""
        accounts = list(self._accounts.values())
        
        return {
            "total_accounts": len(accounts),
            "by_type": {
                t.value: len([a for a in accounts if a.credential_type == t])
                for t in CredentialType
            },
            "by_creator": {
                "user": len([a for a in accounts if a.created_by == "user"]),
                "agent": len([a for a in accounts if a.created_by == "agent"]),
            },
            "sso_accounts": len([a for a in accounts if a.created_via_sso]),
            "with_2fa": len([a for a in accounts if a.has_2fa]),
        }
    
    def format_for_prompt(self, include_types: Optional[List[CredentialType]] = None) -> str:
        """
        Format accounts for inclusion in AI prompt.
        
        Args:
            include_types: Types to include (all if None)
        
        Returns:
            Formatted string for prompt
        """
        accounts = self.list_all()
        
        if include_types:
            accounts = [a for a in accounts if a.credential_type in include_types]
        
        if not accounts:
            return "No registered accounts."
        
        # Group by type
        by_type: Dict[CredentialType, List[AccountMetadata]] = {}
        for acc in accounts:
            by_type.setdefault(acc.credential_type, []).append(acc)
        
        lines = ["Your registered accounts:"]
        
        for cred_type, accs in by_type.items():
            lines.append(f"\n{cred_type.value.title()}s:")
            for acc in accs:
                if acc.created_via_sso:
                    sso_info = f" (via {acc.sso_provider.value})"
                else:
                    sso_info = ""
                lines.append(f"  - {acc.service}: {acc.username}{sso_info}")
        
        return "\n".join(lines)


# Convenience function for creating accounts
def create_account(
    service: str,
    credential_type: str = "website",
    username: str = "",
    email: str = None,
    password_alias: str = None,
    created_by: str = "agent",
    url: str = None,
    app_bundle_id: str = None,
    cli_command: str = None,
    sso_provider: str = None,
    sso_account: str = None,
    has_2fa: bool = False,
    two_factor_method: str = None,
    totp_alias: str = None,
) -> AccountMetadata:
    """
    Create a new account metadata object.
    
    Args:
        service: Service identifier
        credential_type: "website", "app", "cli", "api", "system"
        username: Username for the account
        email: Email (if different from username)
        password_alias: Keychain alias for password
        created_by: "user" or "agent"
        url: URL for websites
        app_bundle_id: Bundle ID for apps
        cli_command: Command for CLI tools
        sso_provider: SSO provider if created via SSO
        sso_account: SSO account used
        has_2fa: Whether 2FA is enabled
        two_factor_method: "totp", "sms", "push"
        totp_alias: Alias for TOTP secret
    
    Returns:
        AccountMetadata instance
    """
    return AccountMetadata(
        name=service.title(),
        credential_type=CredentialType(credential_type),
        service=service,
        url=url,
        app_bundle_id=app_bundle_id,
        cli_command=cli_command,
        username=username,
        email=email,
        password_alias=password_alias,
        created_via_sso=sso_provider is not None,
        sso_provider=SSOProvider(sso_provider) if sso_provider else None,
        sso_account=sso_account,
        has_2fa=has_2fa,
        two_factor_method=TwoFactorMethod(two_factor_method) if two_factor_method else None,
        totp_secret_alias=totp_alias,
        created_by=created_by,
    )


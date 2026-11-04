"""
Secure Credential Vault - Local password storage abstracted from the AI.

Credentials are stored locally and NEVER exposed to the LLM or logs.
The agent can only request to "use" a credential by alias.

Supports two modes:
- USER mode: User's existing credentials (no prefix)
- AGENT mode: Agent-created accounts (prefixed with "agent-service-")
"""
import os
import sys
import json
import getpass
from pathlib import Path
from typing import Optional, List, Dict, Any
from base64 import b64encode, b64decode
from datetime import datetime

try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False

try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

try:
    import pyautogui
    PYAUTOGUI_AVAILABLE = True
except ImportError:
    pyautogui = None  # type: ignore[assignment]
    PYAUTOGUI_AVAILABLE = False

from .accounts import CredentialMode, CredentialType, AccountMetadata, AccountRegistry
from .generator import CredentialGenerator, generate_secure_password
from .sso import SSOProvider, SSOConfig, SSOManager
from .two_factor import TwoFactorHandler, TwoFactorConfig, TwoFactorMethod
from anyuse.paths import anyuse_secret_dir, typing_suppression_flag


def _select_all(chord=None) -> None:
    """Command+A in the focused field. On macOS by the key that types a on the layout in use
    (``context.keyboard.press_chord``, with its interlock check): pyautogui's "a" is the US key
    position, which is Command+Q on AZERTY. Raises when the chord is not sent; pyautogui off macOS."""
    if sys.platform != "darwin":
        pyautogui.hotkey("command", "a")
        return
    if chord is None:
        from anyuse.context.keyboard import press_chord as chord
    out = chord("cmd+a") or {}
    if not out.get("sent"):
        raise RuntimeError(f"could not clear the field: {out.get('refused') or out.get('error') or 'select-all was not sent'}")


class CredentialVault:
    """
    Secure local credential storage.
    
    Supports three storage backends:
    1. Session (in-memory, lost on exit)
    2. Encrypted vault file (persistent, local)
    3. macOS Keychain (persistent, system-level)
    
    Credentials are identified by aliases. The agent can request
    to "use" a credential by alias, but never sees the actual value.
    
    Usage:
        vault = CredentialVault()
        vault.set_credential("github_pass", "secret123", persist=True)
        vault.use_credential("github_pass")  # Types at cursor
    """
    
    SERVICE_NAME = "anyuse"
    AGENT_PREFIX = "agent-service-"  # Prefix for agent-mode credentials
    VAULT_DIR = anyuse_secret_dir()
    VAULT_FILE = VAULT_DIR / "vault.enc"
    ALIASES_FILE = VAULT_DIR / "aliases.json"  # Track what's in keychain
    # Touched while a secret is typed at the cursor so the watcher drops input
    # events for the window (never_captured). The path
    # is the watcher's too: paths.typing_suppression_flag(), read at call time.
    TYPING_SUPPRESSION_FLAG = property(lambda self: typing_suppression_flag())
    
    def __init__(
        self,
        use_keychain: bool = True,
        master_password: Optional[str] = None,
        mode: CredentialMode = CredentialMode.USER,
        sso_config: Optional[SSOConfig] = None,
    ):
        """
        Initialize the credential vault.
        
        Args:
            use_keychain: Whether to use macOS Keychain for persistent storage
            master_password: Password for encrypted vault file (optional)
            mode: Credential mode (USER or AGENT)
            sso_config: Optional SSO configuration
        """
        self.use_keychain = use_keychain and KEYRING_AVAILABLE
        self.mode = mode
        self.session_credentials: Dict[str, str] = {}
        self.master_password = master_password
        self._fernet = None
        self._keychain_aliases: List[str] = []
        
        # SSO and account management
        self.sso_config = sso_config
        self.sso_manager = SSOManager(
            sso_config.provider,
            sso_config.account_email,
            sso_config.credential_alias,
            sso_config.two_factor,
        ) if sso_config else None
        
        # Account registry and generators
        self.account_registry = AccountRegistry()
        self.credential_generator = CredentialGenerator(
            prefix="anyuse-agent" if mode == CredentialMode.AGENT else "user",
        )
        
        # 2FA handler
        self.two_factor = TwoFactorHandler(
            get_secret_func=self.get_credential,
            save_secret_func=lambda alias, val: self.set_credential(alias, val, persist=True),
            verbose=False,
        )
        
        # Ensure directory exists
        self.VAULT_DIR.mkdir(parents=True, exist_ok=True)
        
        # Load keychain aliases list
        self._load_keychain_aliases()
        
        # Initialize encryption if password provided
        if CRYPTO_AVAILABLE and master_password:
            self._init_encryption(master_password)
    
    def _init_encryption(self, password: str):
        """Initialize Fernet encryption with password-derived key."""
        # Use a static salt (in production, store per-user salt)
        salt = b'anyuse_vault_salt_v1'
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480000,
        )
        key = b64encode(kdf.derive(password.encode()))
        self._fernet = Fernet(key)
    
    def _load_keychain_aliases(self):
        """Load list of keychain aliases from tracking file."""
        if self.ALIASES_FILE.exists():
            try:
                with open(self.ALIASES_FILE) as f:
                    data = json.load(f)
                    self._keychain_aliases = data.get("keychain", [])
            except Exception:
                self._keychain_aliases = []
    
    def _save_keychain_aliases(self):
        """Save keychain aliases list."""
        with open(self.ALIASES_FILE, 'w') as f:
            json.dump({"keychain": self._keychain_aliases}, f)
    
    def set_credential(
        self,
        alias: str,
        value: str,
        persist: bool = False,
        use_keychain: bool = True
    ) -> bool:
        """
        Store a credential with an alias.
        
        Args:
            alias: Friendly name for the credential (e.g., "gmail_password")
            value: The actual credential value (NEVER logged or sent to AI)
            persist: Whether to persist to disk/keychain
            use_keychain: Whether to use keychain (if available) for persistence
        
        Returns:
            True if stored successfully.
        """
        # Validate alias
        if not alias.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid alias: {alias}. Use alphanumeric, _, or -")
        
        # Try keychain first for persistent storage
        if persist and use_keychain and self.use_keychain:
            try:
                keyring.set_password(self.SERVICE_NAME, alias, value)
                if alias not in self._keychain_aliases:
                    self._keychain_aliases.append(alias)
                    self._save_keychain_aliases()
                return True
            except Exception as e:
                print(f"Warning: Keychain storage failed: {e}")
                # Fall through to encrypted file
        
        # Try encrypted file for persistent storage
        if persist and self._fernet:
            self._save_to_encrypted_file(alias, value)
            return True
        
        # Session-only storage (in-memory)
        self.session_credentials[alias] = value
        return True
    
    def get_credential(self, alias: str) -> Optional[str]:
        """
        Get a credential value.
        
        WARNING: Only use internally for typing. Never expose to logs or AI!
        """
        # Check session first (fastest)
        if alias in self.session_credentials:
            return self.session_credentials[alias]
        
        # Check keychain
        if self.use_keychain and alias in self._keychain_aliases:
            try:
                value = keyring.get_password(self.SERVICE_NAME, alias)
                if value:
                    return value
            except Exception:
                pass
        
        # Check encrypted file
        if self._fernet:
            value = self._load_from_encrypted_file(alias)
            if value:
                return value
        
        # Check environment variable
        env_key = f"AnyUse_CRED_{alias.upper()}"
        if env_key in os.environ:
            return os.environ[env_key]
        
        return None
    
    def use_credential(
        self,
        alias: str,
        press_enter: bool = False,
        clear_first: bool = False
    ) -> Dict[str, Any]:
        """
        Type the credential at the current cursor position.
        
        This is the ONLY way the agent should use credentials.
        The actual value is never exposed to logs or AI.
        
        Args:
            alias: The credential alias to type
            press_enter: Whether to press Enter after typing
            clear_first: Whether to clear the field first (Cmd+A, Delete)
        
        Returns:
            Result dict (without the credential value!)
        """
        # typing is desktop input: the same switch as every other key and click
        from anyuse.execution.actions import drive_enabled
        if not drive_enabled():
            return {
                "success": False,
                "action": "use_credential",
                "error": "desktop input is off (ANYUSE_DRIVE_ENABLED), so nothing was typed",
            }

        if not PYAUTOGUI_AVAILABLE:
            return {
                "success": False,
                "action": "use_credential",
                "error": (
                    "pyautogui is not installed; cannot type credential at cursor. "
                    "Install it with `pip install pyautogui` to use this feature."
                ),
            }

        value = self.get_credential(alias)

        if value is None:
            return {
                "success": False,
                "action": "use_credential",
                "error": f"Credential '{alias}' not found"
            }

        try:
            # Signal the watcher to suppress input capture while the secret is
            # typed — its keystrokes must never reach any capture buffer.
            try:
                flag = self.TYPING_SUPPRESSION_FLAG
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text(datetime.now().isoformat())
            except Exception:
                pass

            # Optionally clear field first
            if clear_first:
                _select_all()
                pyautogui.press("backspace")

            # Type the credential (value never exposed!)
            pyautogui.write(value, interval=0.02)

            if press_enter:
                pyautogui.press("return")

            return {
                "success": True,
                "action": "use_credential",
                "alias": alias,
                "chars_typed": len(value)  # Only reveal length, not content
            }
        except Exception as e:
            return {
                "success": False,
                "action": "use_credential",
                "error": str(e)
            }
        finally:
            try:
                self.TYPING_SUPPRESSION_FLAG.unlink()
            except Exception:
                pass
    
    def list_aliases(self) -> List[str]:
        """
        Get list of available credential aliases.
        
        Safe to expose to AI - only returns names, not values.
        """
        aliases = set(self.session_credentials.keys())
        
        # Add keychain aliases
        aliases.update(self._keychain_aliases)
        
        # Add encrypted file aliases
        if self._fernet and self.VAULT_FILE.exists():
            try:
                with open(self.VAULT_FILE) as f:
                    data = json.load(f)
                    aliases.update(data.keys())
            except Exception:
                pass
        
        return sorted(aliases)
    
    def remove_credential(self, alias: str) -> bool:
        """Remove a credential by alias."""
        removed = False
        
        # Remove from session
        if alias in self.session_credentials:
            del self.session_credentials[alias]
            removed = True
        
        # Remove from keychain
        if self.use_keychain and alias in self._keychain_aliases:
            try:
                keyring.delete_password(self.SERVICE_NAME, alias)
                self._keychain_aliases.remove(alias)
                self._save_keychain_aliases()
                removed = True
            except Exception:
                pass
        
        # Remove from encrypted file
        if self._fernet and self.VAULT_FILE.exists():
            try:
                with open(self.VAULT_FILE) as f:
                    data = json.load(f)
                if alias in data:
                    del data[alias]
                    with open(self.VAULT_FILE, 'w') as f:
                        json.dump(data, f)
                    removed = True
            except Exception:
                pass
        
        return removed
    
    def _save_to_encrypted_file(self, alias: str, value: str):
        """Save encrypted credential to file."""
        data = {}
        if self.VAULT_FILE.exists():
            try:
                with open(self.VAULT_FILE) as f:
                    data = json.load(f)
            except Exception:
                pass
        
        # Encrypt the value
        encrypted = self._fernet.encrypt(value.encode()).decode()
        data[alias] = encrypted
        
        with open(self.VAULT_FILE, 'w') as f:
            json.dump(data, f)
    
    def _load_from_encrypted_file(self, alias: str) -> Optional[str]:
        """Load and decrypt credential from file."""
        if not self.VAULT_FILE.exists():
            return None
        
        try:
            with open(self.VAULT_FILE) as f:
                data = json.load(f)
            
            if alias not in data:
                return None
            
            encrypted = data[alias]
            return self._fernet.decrypt(encrypted.encode()).decode()
        except Exception:
            return None
    
    # ─────────────────────────────────────────────────────────
    # Agent Mode: Account Creation
    # ─────────────────────────────────────────────────────────
    
    def create_account(
        self,
        service: str,
        credential_type: CredentialType = CredentialType.WEBSITE,
        username: str = None,
        email: str = None,
        url: str = None,
        app_bundle_id: str = None,
        use_sso: bool = True,
    ) -> AccountMetadata:
        """
        Create a new account for a service (agent mode).
        
        Generates secure credentials and registers the account.
        
        Args:
            service: Service name/domain
            credential_type: Type of credential
            username: Username (auto-generated if None)
            email: Email (auto-generated if None)
            url: URL for websites
            app_bundle_id: Bundle ID for apps
            use_sso: Whether to check for SSO availability
        
        Returns:
            AccountMetadata for the new account
        """
        # Check if we should use SSO
        created_via_sso = False
        sso_provider = None
        sso_account = None
        password_alias = None
        
        if use_sso and self.sso_config:
            # In actual usage, the AI would check if SSO is available
            # Here we just set up the account as potentially SSO-based
            created_via_sso = True
            sso_provider = self.sso_config.provider
            sso_account = self.sso_config.account_email
        else:
            # Generate credentials
            creds = self.credential_generator.generate_credentials(service)
            username = username or creds["username"]
            email = email or creds["email"]
            
            # Generate and store password
            password = creds["password"]
            password_alias = self._make_alias(service)
            self.set_credential(password_alias, password, persist=True)
        
        # Create account metadata
        account = AccountMetadata(
            name=service.title(),
            credential_type=credential_type,
            service=service,
            url=url or (f"https://{service}" if credential_type == CredentialType.WEBSITE else None),
            app_bundle_id=app_bundle_id,
            username=username or "",
            email=email,
            password_alias=password_alias,
            created_via_sso=created_via_sso,
            sso_provider=sso_provider,
            sso_account=sso_account,
            created_by="agent",
        )
        
        # Register the account
        self.account_registry.register(account)
        
        return account
    
    def _make_alias(self, service: str) -> str:
        """Generate a credential alias for a service."""
        service_clean = "".join(c for c in service.lower() if c.isalnum() or c == ".")
        
        if self.mode == CredentialMode.AGENT:
            return f"{self.AGENT_PREFIX}{service_clean}"
        return service_clean
    
    def has_account(self, service: str) -> bool:
        """Check if we have an account for a service."""
        return self.account_registry.has_account(service)
    
    def get_account(self, service: str) -> Optional[AccountMetadata]:
        """Get account metadata for a service."""
        return self.account_registry.get(service)
    
    def list_accounts(
        self,
        credential_type: CredentialType = None,
        created_by: str = None,
    ) -> List[AccountMetadata]:
        """
        List accounts with optional filtering.
        
        Args:
            credential_type: Filter by type
            created_by: Filter by creator ("user" or "agent")
        
        Returns:
            List of matching accounts
        """
        accounts = self.account_registry.list_all()
        
        if credential_type:
            accounts = [a for a in accounts if a.credential_type == credential_type]
        
        if created_by:
            accounts = [a for a in accounts if a.created_by == created_by]
        
        return accounts
    
    # ─────────────────────────────────────────────────────────
    # 2FA Support
    # ─────────────────────────────────────────────────────────
    
    def set_2fa_secret(self, alias: str, secret: str) -> bool:
        """
        Store a TOTP secret for an account.
        
        Args:
            alias: Credential alias (e.g., "github-totp")
            secret: Base32-encoded TOTP secret
        
        Returns:
            True if stored successfully
        """
        return self.set_credential(f"{alias}-totp", secret, persist=True)
    
    def get_2fa_code(self, alias: str) -> Optional[str]:
        """
        Get current TOTP code for an account.
        
        Args:
            alias: Account alias
        
        Returns:
            6-digit TOTP code or None
        """
        totp_alias = f"{alias}-totp"
        return self.two_factor.totp.get_code_for_alias(totp_alias)
    
    def read_sms_code(
        self,
        sender: str = None,
        wait: bool = True,
        timeout: int = 120,
    ) -> Optional[str]:
        """
        Read verification code from SMS (Messages app).
        
        Args:
            sender: Expected sender (optional filter)
            wait: Whether to wait for new message
            timeout: Seconds to wait
        
        Returns:
            Verification code or None
        """
        if wait:
            return self.two_factor.sms.wait_for_code(
                sender=sender,
                timeout=timeout,
            )
        else:
            return self.two_factor.sms.get_latest_code(sender=sender)
    
    def set_backup_codes(self, alias: str, codes: List[str]) -> bool:
        """
        Store backup/recovery codes for an account.
        
        Args:
            alias: Account alias
            codes: List of backup codes
        
        Returns:
            True if stored successfully
        """
        codes_str = "\n".join(codes)
        return self.set_credential(f"{alias}-backup", codes_str, persist=True)
    
    def get_backup_code(self, alias: str) -> Optional[str]:
        """
        Get and mark used the next backup code.
        
        Args:
            alias: Account alias
        
        Returns:
            Backup code or None
        """
        backup_alias = f"{alias}-backup"
        return self.two_factor.backup.get_next_code(backup_alias)
    
    # ─────────────────────────────────────────────────────────
    # SSO Support
    # ─────────────────────────────────────────────────────────
    
    def get_sso_instructions(self) -> str:
        """Get SSO instructions for AI prompt."""
        if self.sso_manager:
            return self.sso_manager.get_prompt_instructions()
        return ""
    
    def get_accounts_prompt(self) -> str:
        """Get formatted accounts list for AI prompt."""
        return self.account_registry.format_for_prompt()


def prompt_for_credential(alias: str, persist: bool = False) -> bool:
    """
    Interactive prompt to set a credential.
    
    Args:
        alias: Name for the credential
        persist: Whether to persist to disk
    
    Returns:
        True if credential was set.
    """
    vault = CredentialVault()
    
    print(f"\nSetting credential: {alias}")
    value = getpass.getpass("Enter value (hidden): ")
    
    if not value:
        print("No value entered, cancelled.")
        return False
    
    confirm = getpass.getpass("Confirm value: ")
    
    if value != confirm:
        print("Values don't match, cancelled.")
        return False
    
    vault.set_credential(alias, value, persist=persist)
    storage = "persistent" if persist else "session only"
    print(f"✓ Credential '{alias}' saved ({storage})")
    return True


if __name__ == "__main__":
    # Test the vault
    print("Testing Credential Vault...")
    
    vault = CredentialVault(use_keychain=False)
    
    # Test session storage
    vault.set_credential("test_session", "secret123")
    print(f"Aliases: {vault.list_aliases()}")
    
    # Verify we can retrieve it
    value = vault.get_credential("test_session")
    print(f"Retrieved value length: {len(value) if value else 0}")
    
    # Test use_credential (would type at cursor)
    print("\nTesting use_credential...")
    result = vault.use_credential("test_session")
    print(f"Use result: {result}")


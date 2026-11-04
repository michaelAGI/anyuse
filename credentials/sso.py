"""
SSO (Single Sign-On) Support

Handles SSO provider configuration and detection for:
- Google
- Microsoft
- Apple
- GitHub
- Facebook
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict

from .two_factor import TwoFactorConfig, TwoFactorMethod


class SSOProvider(Enum):
    """Supported SSO providers."""
    GOOGLE = "google"
    MICROSOFT = "microsoft"
    APPLE = "apple"
    GITHUB = "github"
    FACEBOOK = "facebook"
    NONE = "none"  # Disable SSO preference


# Default button text patterns for each provider
# NOTE: These are kept for optional configuration/reference.
# The AI model primarily uses visual recognition to detect SSO buttons,
# not hardcoded text matching.
DEFAULT_SSO_PATTERNS: Dict[SSOProvider, List[str]] = {
    SSOProvider.GOOGLE: [
        "Sign in with Google",
        "Continue with Google",
        "Log in with Google",
        "Google",
        "Mit Google anmelden",  # German
        "Weiter mit Google",
    ],
    SSOProvider.MICROSOFT: [
        "Sign in with Microsoft",
        "Continue with Microsoft",
        "Microsoft account",
        "Log in with Microsoft",
        "Microsoft",
        "Mit Microsoft anmelden",
    ],
    SSOProvider.APPLE: [
        "Sign in with Apple",
        "Continue with Apple",
        "Log in with Apple",
        "Apple",
        "Mit Apple anmelden",
    ],
    SSOProvider.GITHUB: [
        "Sign in with GitHub",
        "Continue with GitHub",
        "Log in with GitHub",
        "GitHub",
        "Mit GitHub anmelden",
    ],
    SSOProvider.FACEBOOK: [
        "Sign in with Facebook",
        "Continue with Facebook",
        "Log in with Facebook",
        "Facebook",
        "Mit Facebook anmelden",
    ],
}


@dataclass
class SSOConfig:
    """
    Configuration for SSO authentication.
    
    Attributes:
        provider: Which SSO provider to prefer
        account_email: Email/account to use for SSO
        credential_alias: Keychain alias for SSO provider password
        two_factor: Optional 2FA configuration for SSO provider
        button_texts: Custom button text patterns to look for
    """
    provider: SSOProvider
    account_email: str
    credential_alias: str
    two_factor: Optional[TwoFactorConfig] = None
    button_texts: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        """Set default button texts if not provided."""
        if not self.button_texts and self.provider != SSOProvider.NONE:
            self.button_texts = DEFAULT_SSO_PATTERNS.get(self.provider, [])
    
    def to_dict(self) -> dict:
        return {
            "provider": self.provider.value,
            "account_email": self.account_email,
            "credential_alias": self.credential_alias,
            "two_factor": self.two_factor.to_dict() if self.two_factor else None,
            "button_texts": self.button_texts,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "SSOConfig":
        two_factor = None
        if data.get("two_factor"):
            two_factor = TwoFactorConfig.from_dict(data["two_factor"])
        
        return cls(
            provider=SSOProvider(data["provider"]),
            account_email=data["account_email"],
            credential_alias=data["credential_alias"],
            two_factor=two_factor,
            button_texts=data.get("button_texts", []),
        )


class SSOManager:
    """
    Manages SSO configuration and provides instructions for AI.
    
    Usage:
        sso = SSOManager(
            preferred=SSOProvider.GOOGLE,
            account="agent@gmail.com",
            credential_alias="agent-sso-google",
        )
        
        # Get instructions for AI prompt
        instructions = sso.get_prompt_instructions()
    """
    
    def __init__(
        self,
        preferred: SSOProvider,
        account: str,
        credential_alias: str,
        two_factor: Optional[TwoFactorConfig] = None,
    ):
        """
        Initialize SSO manager.
        
        Args:
            preferred: Preferred SSO provider
            account: Email/account for SSO
            credential_alias: Keychain alias for password
            two_factor: Optional 2FA config
        """
        self.config = SSOConfig(
            provider=preferred,
            account_email=account,
            credential_alias=credential_alias,
            two_factor=two_factor,
        )
    
    @property
    def provider(self) -> SSOProvider:
        return self.config.provider
    
    @property
    def account(self) -> str:
        return self.config.account_email
    
    @property
    def button_texts(self) -> List[str]:
        return self.config.button_texts
    
    def get_prompt_instructions(self) -> str:
        """
        Get instructions for AI about SSO preference.
        
        AI visually detects SSO options - no hardcoded button patterns needed.
        
        Returns:
            Instruction text to include in AI prompt
        """
        if self.provider == SSOProvider.NONE:
            return ""
        
        provider_name = self.provider.value.title()
        
        instructions = f"""
SSO PREFERENCE: {provider_name}
When creating accounts or logging in, prefer "{provider_name}" sign-in if available.

Account to use: {self.account}

SSO Login Flow:
1. Visually scan the page for "{provider_name}" sign-in button/link
2. If found, click it
3. If an account picker appears, select: {self.account}
4. If already logged into {provider_name}, it may auto-complete
5. Complete any authorization/permission prompts

If 2FA is required for {provider_name}:
"""
        
        if self.config.two_factor:
            method = self.config.two_factor.method.value.upper()
            instructions += f"- Method: {method}\n"
            
            if self.config.two_factor.method == TwoFactorMethod.TOTP:
                instructions += "- Use get_2fa_code() to get the current code\n"
            elif self.config.two_factor.method == TwoFactorMethod.SMS:
                instructions += "- Use read_sms_code() to read code from Messages\n"
            elif self.config.two_factor.method == TwoFactorMethod.PUSH:
                instructions += "- Use wait_for_push_approval() and wait for user\n"
        else:
            instructions += "- TOTP: Use get_2fa_code() for the code\n"
            instructions += "- SMS: Use read_sms_code() to read from Messages\n"
            instructions += "- Push: Wait for user approval on phone\n"
        
        instructions += f"""
If "{provider_name}" sign-in is NOT available:
- Create account with email/password instead
- Generate a secure password
- Save the new credentials
"""
        
        return instructions
    
    def get_detection_hints(self) -> Dict[str, any]:
        """
        Get hints for detecting SSO options on a page.
        
        Returns:
            Dict with detection information
        """
        return {
            "provider": self.provider.value,
            "button_texts": self.button_texts,
            "button_classes": [
                # Common CSS classes for SSO buttons
                f"{self.provider.value}-login",
                f"{self.provider.value}-signin",
                f"btn-{self.provider.value}",
                f"social-{self.provider.value}",
                "social-login",
                "sso-button",
            ],
            "icon_patterns": [
                # Common icon identifiers
                f"{self.provider.value}-icon",
                f"fa-{self.provider.value}",
            ],
        }


def create_sso_config(
    provider: str,
    account: str,
    credential_alias: str = None,
    two_factor_method: str = None,
    totp_alias: str = None,
) -> SSOConfig:
    """
    Helper to create SSO config from simple parameters.
    
    Args:
        provider: "google", "microsoft", "apple", "github", "facebook"
        account: Email/account for SSO
        credential_alias: Keychain alias (auto-generated if None)
        two_factor_method: "totp", "sms", "push" (optional)
        totp_alias: Alias for TOTP secret (if using TOTP)
    
    Returns:
        SSOConfig instance
    """
    sso_provider = SSOProvider(provider.lower())
    
    # Auto-generate credential alias
    if not credential_alias:
        credential_alias = f"agent-sso-{provider.lower()}"
    
    # Create 2FA config if specified
    two_factor = None
    if two_factor_method:
        two_factor = TwoFactorConfig(
            method=TwoFactorMethod(two_factor_method.lower()),
            totp_secret_alias=totp_alias,
        )
    
    return SSOConfig(
        provider=sso_provider,
        account_email=account,
        credential_alias=credential_alias,
        two_factor=two_factor,
    )


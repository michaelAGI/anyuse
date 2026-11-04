"""
Two-Factor Authentication Support

Handles various 2FA methods:
- TOTP: Time-based codes via pyotp (secrets stored in keychain)
- SMS: Read codes from macOS Messages app
- Push: Wait for user approval
- Email: Read codes from Mail app
- Backup: Use stored recovery codes
"""
import logging
import re
import time
import subprocess
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

try:
    import pyotp
    PYOTP_AVAILABLE = True
except ImportError:
    PYOTP_AVAILABLE = False


class TwoFactorMethod(Enum):
    """Supported 2FA methods."""
    TOTP = "totp"       # Time-based one-time passwords
    SMS = "sms"         # SMS codes via Messages app
    PUSH = "push"       # Push notification (wait for approval)
    EMAIL = "email"     # Email codes (read from Mail app)
    BACKUP = "backup"   # Backup/recovery codes


@dataclass
class TwoFactorConfig:
    """Configuration for 2FA on an account."""
    method: TwoFactorMethod
    totp_secret_alias: Optional[str] = None     # Keychain alias for TOTP secret
    sms_sender: Optional[str] = None            # Expected SMS sender
    email_sender: Optional[str] = None          # Expected email sender
    backup_codes_alias: Optional[str] = None    # Keychain alias for backup codes
    push_timeout: int = 120                     # Seconds to wait for push approval
    
    def to_dict(self) -> dict:
        return {
            "method": self.method.value,
            "totp_secret_alias": self.totp_secret_alias,
            "sms_sender": self.sms_sender,
            "email_sender": self.email_sender,
            "backup_codes_alias": self.backup_codes_alias,
            "push_timeout": self.push_timeout,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "TwoFactorConfig":
        return cls(
            method=TwoFactorMethod(data["method"]),
            totp_secret_alias=data.get("totp_secret_alias"),
            sms_sender=data.get("sms_sender"),
            email_sender=data.get("email_sender"),
            backup_codes_alias=data.get("backup_codes_alias"),
            push_timeout=data.get("push_timeout", 120),
        )


class TOTPGenerator:
    """
    Generate TOTP codes from stored secrets.
    
    Secrets should be stored securely in keychain, referenced by alias.
    """
    
    def __init__(self, get_secret_func=None):
        """
        Initialize TOTP generator.
        
        Args:
            get_secret_func: Function to retrieve secret by alias (e.g., keychain.get)
        """
        self._get_secret = get_secret_func
        
        if not PYOTP_AVAILABLE:
            # only a TOTP step needs it; that step raises "pyotp not installed" (get_code)
            logger.debug("pyotp not installed: TOTP generation disabled")
    
    def get_code(self, secret: str) -> str:
        """
        Generate current TOTP code from a secret.
        
        Args:
            secret: Base32-encoded TOTP secret
        
        Returns:
            6-digit TOTP code
        """
        if not PYOTP_AVAILABLE:
            raise RuntimeError("pyotp not installed")
        
        totp = pyotp.TOTP(secret)
        return totp.now()
    
    def get_code_for_alias(self, alias: str) -> str:
        """
        Generate TOTP code for a stored secret alias.
        
        Args:
            alias: Keychain alias for the TOTP secret
        
        Returns:
            6-digit TOTP code
        """
        if not self._get_secret:
            raise RuntimeError("No secret retrieval function configured")
        
        secret = self._get_secret(alias)
        if not secret:
            raise ValueError(f"No TOTP secret found for alias: {alias}")
        
        return self.get_code(secret)
    
    def verify_code(self, secret: str, code: str) -> bool:
        """
        Verify a TOTP code.
        
        Args:
            secret: Base32-encoded TOTP secret
            code: Code to verify
        
        Returns:
            True if code is valid
        """
        if not PYOTP_AVAILABLE:
            raise RuntimeError("pyotp not installed")
        
        totp = pyotp.TOTP(secret)
        return totp.verify(code)
    
    def get_time_remaining(self, secret: str) -> int:
        """
        Get seconds until current code expires.
        
        Args:
            secret: Base32-encoded TOTP secret
        
        Returns:
            Seconds remaining (0-30)
        """
        if not PYOTP_AVAILABLE:
            return 0
        
        totp = pyotp.TOTP(secret)
        return totp.interval - (int(time.time()) % totp.interval)


class SMSCodeReader:
    """
    Read SMS verification codes from macOS Messages app.
    
    Uses AppleScript to access recent messages and extract verification codes.
    Requires Messages app to be set up with iPhone SMS forwarding.
    """
    
    # Pattern to match verification codes (4-8 digits)
    CODE_PATTERN = re.compile(r'\b(\d{4,8})\b')
    
    # Common code prefixes in messages
    CODE_KEYWORDS = [
        "code", "verification", "verify", "otp", "password",
        "pin", "security", "confirm", "token", "auth"
    ]
    
    # AppleScript to get recent messages
    APPLESCRIPT_GET_MESSAGES = '''
    tell application "Messages"
        set output to ""
        set chatList to every chat
        repeat with aChat in chatList
            set msgList to messages of aChat
            set msgCount to count of msgList
            if msgCount > 0 then
                -- Get last N messages
                set startIdx to msgCount - {limit} + 1
                if startIdx < 1 then set startIdx to 1
                repeat with i from startIdx to msgCount
                    set aMsg to item i of msgList
                    set msgDate to date received of aMsg
                    set msgText to text of aMsg
                    set msgSender to sender of aMsg
                    set output to output & msgDate & "|||" & msgSender & "|||" & msgText & "
"
                end repeat
            end if
        end repeat
        return output
    end tell
    '''
    
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
    
    def get_recent_messages(self, limit: int = 10) -> List[dict]:
        """
        Get recent messages from Messages app.
        
        Args:
            limit: Maximum messages to retrieve per chat
        
        Returns:
            List of message dicts with 'date', 'sender', 'text'
        """
        script = self.APPLESCRIPT_GET_MESSAGES.replace("{limit}", str(limit))
        
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                if self.verbose:
                    print(f"AppleScript error: {result.stderr}")
                return []
            
            messages = []
            for line in result.stdout.strip().split("\n"):
                if "|||" in line:
                    parts = line.split("|||")
                    if len(parts) >= 3:
                        messages.append({
                            "date": parts[0].strip(),
                            "sender": parts[1].strip(),
                            "text": parts[2].strip(),
                        })
            
            return messages
            
        except subprocess.TimeoutExpired:
            if self.verbose:
                print("Timeout reading from Messages")
            return []
        except Exception as e:
            if self.verbose:
                print(f"Error reading Messages: {e}")
            return []
    
    def extract_code(self, text: str) -> Optional[str]:
        """
        Extract verification code from message text.
        
        Args:
            text: Message text
        
        Returns:
            Extracted code or None
        """
        text_lower = text.lower()
        
        # Check if message looks like a verification message
        has_keyword = any(kw in text_lower for kw in self.CODE_KEYWORDS)
        
        # Find all digit sequences
        codes = self.CODE_PATTERN.findall(text)
        
        if codes:
            # If it's a verification message, return the code
            if has_keyword:
                # Prefer 6-digit codes (most common)
                for code in codes:
                    if len(code) == 6:
                        return code
                return codes[0]
            
            # Even without keyword, return if it's a 6-digit standalone code
            for code in codes:
                if len(code) == 6:
                    return code
        
        return None
    
    def get_latest_code(
        self,
        sender: Optional[str] = None,
        since_minutes: int = 5,
    ) -> Optional[str]:
        """
        Get the latest verification code from Messages.
        
        Args:
            sender: Filter by sender (partial match)
            since_minutes: Only consider messages from last N minutes
        
        Returns:
            Verification code or None
        """
        messages = self.get_recent_messages(limit=20)
        
        # Filter by sender if specified
        if sender:
            sender_lower = sender.lower()
            messages = [
                m for m in messages 
                if sender_lower in m.get("sender", "").lower()
            ]
        
        # Look for codes in recent messages (newest first)
        for msg in reversed(messages):
            code = self.extract_code(msg.get("text", ""))
            if code:
                if self.verbose:
                    print(f"Found code: {code} from {msg.get('sender')}")
                return code
        
        return None
    
    def wait_for_code(
        self,
        sender: Optional[str] = None,
        timeout: int = 120,
        poll_interval: int = 3,
    ) -> Optional[str]:
        """
        Wait for a new verification code to arrive.
        
        Args:
            sender: Filter by sender
            timeout: Maximum seconds to wait
            poll_interval: Seconds between checks
        
        Returns:
            Verification code or None if timeout
        """
        if self.verbose:
            print(f"Waiting for SMS code (timeout: {timeout}s)...")
        
        # Get initial codes to ignore
        initial_code = self.get_latest_code(sender=sender)
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            current_code = self.get_latest_code(sender=sender)
            
            # Found a new code
            if current_code and current_code != initial_code:
                if self.verbose:
                    print(f"New code received: {current_code}")
                return current_code
            
            time.sleep(poll_interval)
        
        if self.verbose:
            print("Timeout waiting for SMS code")
        return None


class PushApprovalWaiter:
    """
    Wait for push notification approval.
    
    Used when 2FA requires approving a push notification on another device.
    """
    
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
    
    def wait_for_approval(
        self,
        check_func,
        timeout: int = 120,
        poll_interval: int = 5,
        message: str = "Please approve the login on your phone...",
    ) -> bool:
        """
        Wait for push approval by polling a check function.
        
        Args:
            check_func: Function that returns True when approved
            timeout: Maximum seconds to wait
            poll_interval: Seconds between checks
            message: Message to display while waiting
        
        Returns:
            True if approved, False if timeout
        """
        if self.verbose:
            print(message)
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            if check_func():
                if self.verbose:
                    print("Push notification approved!")
                return True
            
            remaining = int(timeout - (time.time() - start_time))
            if self.verbose and remaining % 30 == 0:
                print(f"  Waiting... ({remaining}s remaining)")
            
            time.sleep(poll_interval)
        
        if self.verbose:
            print("Timeout waiting for push approval")
        return False


class BackupCodeManager:
    """
    Manage backup/recovery codes.
    
    Stores codes and tracks which ones have been used.
    """
    
    def __init__(self, get_codes_func=None, save_codes_func=None):
        """
        Initialize backup code manager.
        
        Args:
            get_codes_func: Function to retrieve codes by alias
            save_codes_func: Function to save updated codes
        """
        self._get_codes = get_codes_func
        self._save_codes = save_codes_func
    
    def get_next_code(self, alias: str) -> Optional[str]:
        """
        Get the next unused backup code.
        
        Args:
            alias: Keychain alias for backup codes
        
        Returns:
            Next unused code or None if all used
        """
        if not self._get_codes:
            raise RuntimeError("No code retrieval function configured")
        
        codes_data = self._get_codes(alias)
        if not codes_data:
            return None
        
        # Parse codes (stored as newline-separated, used ones prefixed with #)
        codes = codes_data.strip().split("\n")
        
        for i, code in enumerate(codes):
            if not code.startswith("#"):
                return code.strip()
        
        return None  # All codes used
    
    def mark_code_used(self, alias: str, code: str) -> bool:
        """
        Mark a backup code as used.
        
        Args:
            alias: Keychain alias
            code: Code that was used
        
        Returns:
            True if successfully marked
        """
        if not self._get_codes or not self._save_codes:
            return False
        
        codes_data = self._get_codes(alias)
        if not codes_data:
            return False
        
        # Mark the code as used
        codes = codes_data.strip().split("\n")
        updated = []
        for c in codes:
            if c.strip() == code:
                updated.append(f"# {code}")  # Mark as used
            else:
                updated.append(c)
        
        self._save_codes(alias, "\n".join(updated))
        return True
    
    def count_remaining(self, alias: str) -> int:
        """Count remaining unused backup codes."""
        if not self._get_codes:
            return 0
        
        codes_data = self._get_codes(alias)
        if not codes_data:
            return 0
        
        codes = codes_data.strip().split("\n")
        return sum(1 for c in codes if not c.startswith("#"))


class TwoFactorHandler:
    """
    Unified handler for all 2FA methods.
    
    Coordinates between TOTP, SMS, Push, and Backup code methods.
    """
    
    def __init__(
        self,
        get_secret_func=None,
        save_secret_func=None,
        verbose: bool = False,
    ):
        self.verbose = verbose
        self.totp = TOTPGenerator(get_secret_func)
        self.sms = SMSCodeReader(verbose=verbose)
        self.push = PushApprovalWaiter(verbose=verbose)
        self.backup = BackupCodeManager(get_secret_func, save_secret_func)
    
    def get_code(
        self,
        config: TwoFactorConfig,
        wait_for_sms: bool = True,
    ) -> Optional[str]:
        """
        Get 2FA code based on configuration.
        
        Args:
            config: 2FA configuration
            wait_for_sms: Whether to wait for SMS (vs just check recent)
        
        Returns:
            Code or None
        """
        if config.method == TwoFactorMethod.TOTP:
            if config.totp_secret_alias:
                return self.totp.get_code_for_alias(config.totp_secret_alias)
            
        elif config.method == TwoFactorMethod.SMS:
            if wait_for_sms:
                return self.sms.wait_for_code(
                    sender=config.sms_sender,
                    timeout=60,
                )
            else:
                return self.sms.get_latest_code(sender=config.sms_sender)
            
        elif config.method == TwoFactorMethod.BACKUP:
            if config.backup_codes_alias:
                return self.backup.get_next_code(config.backup_codes_alias)
        
        return None
    
    def get_prompt_instructions(self) -> str:
        """Get instructions for AI about available 2FA methods."""
        return """
2FA Support Available:
- TOTP: Use get_2fa_code(alias) to get current code
- SMS: Use read_sms_code(sender, wait) to read from Messages app
- Push: Use wait_for_push_approval(timeout) to wait for user
- Backup: Use get_backup_code(alias) to use a recovery code

When you see a 2FA prompt:
1. Identify the 2FA method required
2. Use the appropriate tool to get/wait for the code
3. Enter the code to complete login
"""


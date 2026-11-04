"""
Secure Password and Username Generator

Generates cryptographically secure passwords and unique usernames
for agent-created accounts.
"""
import secrets
import string
import hashlib
from datetime import datetime
from typing import Optional


def generate_secure_password(
    length: int = 32,
    include_uppercase: bool = True,
    include_lowercase: bool = True,
    include_digits: bool = True,
    include_special: bool = True,
    special_chars: str = "!@#$%^&*",
    exclude_ambiguous: bool = True,
) -> str:
    """
    Generate a cryptographically secure random password.
    
    Args:
        length: Password length (default 32)
        include_uppercase: Include A-Z
        include_lowercase: Include a-z
        include_digits: Include 0-9
        include_special: Include special characters
        special_chars: Which special characters to use
        exclude_ambiguous: Exclude ambiguous chars (0O, 1lI)
    
    Returns:
        Secure random password
    """
    alphabet = ""
    
    if include_uppercase:
        chars = string.ascii_uppercase
        if exclude_ambiguous:
            chars = chars.replace("O", "").replace("I", "")
        alphabet += chars
    
    if include_lowercase:
        chars = string.ascii_lowercase
        if exclude_ambiguous:
            chars = chars.replace("l", "")
        alphabet += chars
    
    if include_digits:
        chars = string.digits
        if exclude_ambiguous:
            chars = chars.replace("0", "").replace("1", "")
        alphabet += chars
    
    if include_special:
        alphabet += special_chars
    
    if not alphabet:
        alphabet = string.ascii_letters + string.digits
    
    # Generate password ensuring at least one char from each category
    password_chars = []
    
    if include_uppercase and length >= 4:
        password_chars.append(secrets.choice(string.ascii_uppercase.replace("O", "").replace("I", "") if exclude_ambiguous else string.ascii_uppercase))
    if include_lowercase and length >= 4:
        password_chars.append(secrets.choice(string.ascii_lowercase.replace("l", "") if exclude_ambiguous else string.ascii_lowercase))
    if include_digits and length >= 4:
        password_chars.append(secrets.choice(string.digits.replace("0", "").replace("1", "") if exclude_ambiguous else string.digits))
    if include_special and length >= 4:
        password_chars.append(secrets.choice(special_chars))
    
    # Fill remaining length
    remaining = length - len(password_chars)
    password_chars.extend(secrets.choice(alphabet) for _ in range(remaining))
    
    # Shuffle to randomize positions
    secrets.SystemRandom().shuffle(password_chars)
    
    return "".join(password_chars)


def generate_passphrase(
    word_count: int = 4,
    separator: str = "-",
    capitalize: bool = True,
) -> str:
    """
    Generate a memorable passphrase (e.g., "correct-horse-battery-staple").
    
    Uses a built-in word list for simplicity.
    
    Args:
        word_count: Number of words
        separator: Word separator
        capitalize: Capitalize first letter of each word
    
    Returns:
        Passphrase string
    """
    # Simple word list (in production, use a larger list)
    words = [
        "apple", "brave", "cloud", "dance", "eagle", "flame", "grape", "honey",
        "ivory", "jewel", "karma", "lemon", "mango", "noble", "ocean", "piano",
        "queen", "river", "solar", "tiger", "urban", "vivid", "waltz", "xenon",
        "yacht", "zebra", "amber", "blaze", "coral", "delta", "ember", "frost",
        "glory", "haven", "index", "jolly", "kites", "lunar", "maple", "nexus",
        "orbit", "prism", "quest", "radar", "spark", "torch", "ultra", "vapor",
        "woven", "xylem", "youth", "zesty", "alpha", "bravo", "cyber", "drift",
        "epoch", "focus", "gamma", "haste", "ionic", "jazzy", "kinky", "logic",
    ]
    
    selected = [secrets.choice(words) for _ in range(word_count)]
    
    if capitalize:
        selected = [w.capitalize() for w in selected]
    
    return separator.join(selected)


def generate_username(
    service: str,
    prefix: str = "anyuse-agent",
    include_timestamp: bool = False,
) -> str:
    """
    Generate a unique username for agent accounts.
    
    Args:
        service: Service name (used for hashing)
        prefix: Username prefix
        include_timestamp: Include timestamp in username
    
    Returns:
        Unique username (e.g., "anyuse-agent-a1b2c3d4")
    """
    # Generate random suffix
    suffix = secrets.token_hex(4)  # 8 hex chars
    
    if include_timestamp:
        timestamp = datetime.now().strftime("%y%m%d")
        return f"{prefix}-{timestamp}-{suffix}"
    
    return f"{prefix}-{suffix}"


def generate_email(
    service: str,
    domain: str = "anyuse.local",
    prefix: str = "agent",
) -> str:
    """
    Generate a unique email address for agent accounts.
    
    Args:
        service: Service name (used in email)
        domain: Email domain
        prefix: Email prefix
    
    Returns:
        Email address (e.g., "agent-github-a1b2@anyuse.local")
    
    Note:
        For real usage, you'd want a real domain that can receive emails.
        Consider using email aliases or a catch-all domain.
    """
    # Sanitize service name
    service_clean = "".join(c for c in service.lower() if c.isalnum())
    
    # Generate unique suffix
    suffix = secrets.token_hex(2)  # 4 hex chars
    
    return f"{prefix}-{service_clean}-{suffix}@{domain}"


def generate_api_key(
    prefix: str = "anyuse",
    length: int = 32,
) -> str:
    """
    Generate an API key format string.
    
    Args:
        prefix: Key prefix
        length: Random portion length
    
    Returns:
        API key (e.g., "anyuse_a1b2c3d4e5f6...")
    """
    random_part = secrets.token_hex(length // 2)
    return f"{prefix}_{random_part}"


def generate_recovery_codes(
    count: int = 10,
    code_length: int = 8,
    separator: str = "-",
) -> list:
    """
    Generate backup/recovery codes.
    
    Args:
        count: Number of codes to generate
        code_length: Length of each code segment
        separator: Separator between segments
    
    Returns:
        List of recovery codes
    """
    codes = []
    for _ in range(count):
        # Generate two segments for readability
        seg1 = secrets.token_hex(code_length // 4).upper()
        seg2 = secrets.token_hex(code_length // 4).upper()
        codes.append(f"{seg1}{separator}{seg2}")
    
    return codes


def hash_password(password: str, salt: Optional[str] = None) -> tuple:
    """
    Hash a password using SHA-256 (for storage reference, not actual auth).
    
    Note: For actual password storage, use bcrypt or argon2.
    This is just for generating consistent references.
    
    Args:
        password: Password to hash
        salt: Optional salt (generated if None)
    
    Returns:
        Tuple of (hash, salt)
    """
    if salt is None:
        salt = secrets.token_hex(16)
    
    combined = f"{salt}{password}"
    hash_value = hashlib.sha256(combined.encode()).hexdigest()
    
    return hash_value, salt


def password_strength(password: str) -> dict:
    """
    Evaluate password strength.
    
    Args:
        password: Password to evaluate
    
    Returns:
        Dict with strength metrics
    """
    length = len(password)
    has_upper = any(c.isupper() for c in password)
    has_lower = any(c.islower() for c in password)
    has_digit = any(c.isdigit() for c in password)
    has_special = any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in password)
    
    # Calculate score
    score = 0
    if length >= 8:
        score += 1
    if length >= 12:
        score += 1
    if length >= 16:
        score += 1
    if has_upper:
        score += 1
    if has_lower:
        score += 1
    if has_digit:
        score += 1
    if has_special:
        score += 1
    
    # Determine strength level
    if score >= 6:
        strength = "strong"
    elif score >= 4:
        strength = "medium"
    else:
        strength = "weak"
    
    return {
        "length": length,
        "has_uppercase": has_upper,
        "has_lowercase": has_lower,
        "has_digits": has_digit,
        "has_special": has_special,
        "score": score,
        "max_score": 7,
        "strength": strength,
    }


class CredentialGenerator:
    """
    Unified credential generator for agent accounts.
    
    Usage:
        gen = CredentialGenerator(prefix="my-agent")
        creds = gen.generate_credentials("github.com")
        # {'username': 'my-agent-a1b2c3d4', 'password': '...', 'email': '...'}
    """
    
    def __init__(
        self,
        prefix: str = "anyuse-agent",
        email_domain: str = "anyuse.local",
        password_length: int = 32,
    ):
        """
        Initialize generator.
        
        Args:
            prefix: Prefix for generated usernames
            email_domain: Domain for generated emails
            password_length: Default password length
        """
        self.prefix = prefix
        self.email_domain = email_domain
        self.password_length = password_length
    
    def generate_credentials(
        self,
        service: str,
        include_email: bool = True,
    ) -> dict:
        """
        Generate a complete set of credentials for a service.
        
        Args:
            service: Service name
            include_email: Whether to generate email
        
        Returns:
            Dict with username, password, and optionally email
        """
        result = {
            "username": generate_username(service, prefix=self.prefix),
            "password": generate_secure_password(length=self.password_length),
        }
        
        if include_email:
            result["email"] = generate_email(service, domain=self.email_domain)
        
        return result
    
    def generate_alias(self, service: str, mode: str = "agent") -> str:
        """
        Generate a keychain alias for a credential.
        
        Args:
            service: Service name
            mode: "agent" or "user"
        
        Returns:
            Keychain alias
        """
        service_clean = "".join(c for c in service.lower() if c.isalnum() or c == ".")
        
        if mode == "agent":
            return f"agent-service-{service_clean}"
        else:
            return service_clean


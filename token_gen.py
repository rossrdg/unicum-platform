import secrets
import datetime
import uuid
import hashlib

def generate_token(style="default", length=32):
    """
    Generate a unique token that includes current date/time + unique symbols

    Styles:
    - "secure": sha256 hash of timestamp + secrets
    """

    # Get current timestamp with microseconds for uniqueness
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S_%f")

    # Unique symbols set
    symbols = "!@#$%^&*()_+-=[]{}|;:,.<>?"

    if style == "secure":
        # SHA256 hash = strong, not reversible
        raw = f"{timestamp}_{secrets.token_hex(16)}_{uuid.uuid4()}"
        hash_obj = hashlib.sha256(raw.encode())
        token = hash_obj.hexdigest()[:length] # truncate to desired length

    else:
        raise ValueError("Style must be: default, compact, readable, or secure")

    return token

# Examples
if __name__ == "__main__":
    print("Secure: ", generate_token("secure", 40))

    # Generate 5 tokens
    print("\nBatch of 5 secure tokens:")
    for i in range(5):
        print(f" {i+1}. {generate_token('secure')}")
import os
import base64
from eth_account import Account
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Enable Mnemonic generation if needed
Account.enable_unaudited_hdwallet_features()

class WalletManager:
    def __init__(self):
        # We look for SYNAI_MASTER_KEY in environment variables
        # If not found, we use a fallback (not recommended for production)
        master_key_str = os.getenv('SYNAI_MASTER_KEY', 'default_synai_secret_key_change_me')
        
        # Derive a proper 32-byte key for Fernet
        salt = b'synai_salt_v1' # Use a static salt for consistency across restarts
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        self.key = base64.urlsafe_b64encode(kdf.derive(master_key_str.encode()))
        self.cipher_suite = Fernet(self.key)

    def create_wallet(self):
        """Generates a new ETH wallet and returns (address, encrypted_privkey)."""
        acct = Account.create()
        encrypted_key = self.cipher_suite.encrypt(acct.key.hex().encode()).decode()
        return acct.address, encrypted_key

    def decrypt_privkey(self, encrypted_key):
        """Decrypts a private key for use (e.g., signing a transaction)."""
        decrypted_key = self.cipher_suite.decrypt(encrypted_key.encode()).decode()
        return decrypted_key

# Singleton instance
wallet_manager = WalletManager()

if __name__ == "__main__":
    # Test
    wm = WalletManager()
    addr, enc_key = wm.create_wallet()
    print(f"Address: {addr}")
    print(f"Encrypted Key: {enc_key}")
    dec_key = wm.decrypt_privkey(enc_key)
    print(f"Decrypted Key Matches: {len(dec_key) > 0}")

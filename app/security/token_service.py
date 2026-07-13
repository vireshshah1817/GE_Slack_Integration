import os
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class TokenEncryptionService:


    def __init__(self, master_key_b64: str):

        """
            Initializes the service. 
            master_key_b64 must be a base64-encoded 32-byte (256-bit) key.
        """

        self.key = base64.b64decode(master_key_b64)

        if len(self.key) != 32:
            raise ValueError("AES-256 requires exactly a 32-byte key.")
        
        self.aesgcm = AESGCM(self.key)


    def encrypt(self, plaintext_token: str) -> str:
        """
            Encrypts a token and bundles the IV into a single base64 string.
        """

        # AES-GCM requires a unique 12-byte nonce (IV) for every single encryption
        nonce = os.urandom(12)
        token_bytes = plaintext_token.encode('utf-8')
        
        # Encrypt and authenticate the token
        ciphertext = self.aesgcm.encrypt(nonce, token_bytes, None)
        
        # Prepend the nonce to the ciphertext for easy storage
        encrypted_payload = nonce + ciphertext
        
        # Return as a string safe for a TEXT database column
        return base64.b64encode(encrypted_payload).decode('utf-8')


    def decrypt(self, encrypted_payload_b64: str) -> str:
        """
            Extracts the IV and decrypts the token.
        """

        encrypted_payload = base64.b64decode(encrypted_payload_b64)
        
        # Slice the first 12 bytes to get the nonce, the rest is ciphertext
        nonce = encrypted_payload[:12]
        ciphertext = encrypted_payload[12:]
        
        # Decrypt (this will throw InvalidTag if the data was tampered with)
        plaintext_bytes = self.aesgcm.decrypt(nonce, ciphertext, None)
        
        return plaintext_bytes.decode('utf-8')

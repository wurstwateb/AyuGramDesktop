// This is the source code of TypendruckerGram, based on AyuGram for Desktop.
//
// Encryption of TypendruckerGram Sync, see docs/sync/protocol.md.
// Depends only on OpenSSL and the standard library so it can be tested
// against docs/sync/test-vectors.json outside of the client build.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace AyuSync::Crypto {

using Bytes = std::vector<std::uint8_t>;

inline constexpr int kKeySize = 32;
inline constexpr int kNonceSize = 12;
inline constexpr int kTagSize = 16;

struct KeyPair {
	Bytes privateKey;
	Bytes publicKey;
};

[[nodiscard]] KeyPair GenerateKeyPair();
[[nodiscard]] Bytes PublicFromPrivate(const Bytes &privateKey);

// blob = 0x01 | eph_pub(32) | nonce(12) | ciphertext | tag(16)
[[nodiscard]] Bytes Seal(const Bytes &ownerPublic, const Bytes &plaintext);
// Fixed ephemeral key and nonce, for test vectors only.
[[nodiscard]] Bytes SealWith(
	const Bytes &ownerPublic,
	const Bytes &plaintext,
	const Bytes &ephemeralPrivate,
	const Bytes &nonce);
[[nodiscard]] std::optional<Bytes> Open(const Bytes &ownerPrivate, const Bytes &blob);

// "XXXX-XXXX-XXXX", Crockford base32.
[[nodiscard]] std::string NewPairCode();
// Uppercase, no separators, O->0, I/L->1; nullopt if invalid.
[[nodiscard]] std::optional<std::string> NormalizePairCode(std::string_view code);
// Expects a normalized code.
[[nodiscard]] std::string PairLookupHash(const std::string &normalized);
[[nodiscard]] Bytes PairEncrypt(const std::string &normalized, const Bytes &payload);
[[nodiscard]] Bytes PairEncryptWith(
	const std::string &normalized,
	const Bytes &payload,
	const Bytes &nonce);
[[nodiscard]] std::optional<Bytes> PairDecrypt(
	const std::string &normalized,
	const Bytes &data);

[[nodiscard]] std::string Sha256Hex(std::string_view data);
[[nodiscard]] Bytes RandomBytes(int count);
[[nodiscard]] std::string ToHex(const Bytes &data);
[[nodiscard]] std::optional<Bytes> FromHex(std::string_view hex);

} // namespace AyuSync::Crypto

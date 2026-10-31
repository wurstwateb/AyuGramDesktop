// This is the source code of TypendruckerGram, based on AyuGram for Desktop.
#include "ayu/sync/sync_crypto.h"

#include <openssl/evp.h>
#include <openssl/kdf.h>
#include <openssl/rand.h>

#include <memory>
#include <stdexcept>

namespace AyuSync::Crypto {
namespace {

constexpr std::uint8_t kVersion = 1;
constexpr int kHeaderSize = 1 + kKeySize + kNonceSize;
constexpr std::string_view kAad = "typsync";
constexpr std::string_view kEventInfo = "typsync v1 event";
constexpr std::string_view kPairSalt = "typsync pair";
constexpr std::string_view kPairInfo = "typsync v1 pair";
constexpr std::string_view kPairLookupPrefix = "typsync pair lookup|";
constexpr std::string_view kCodeAlphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
constexpr int kCodeLength = 12;

struct PkeyDeleter {
	void operator()(EVP_PKEY *key) const { EVP_PKEY_free(key); }
};
struct PkeyCtxDeleter {
	void operator()(EVP_PKEY_CTX *ctx) const { EVP_PKEY_CTX_free(ctx); }
};
struct CipherCtxDeleter {
	void operator()(EVP_CIPHER_CTX *ctx) const { EVP_CIPHER_CTX_free(ctx); }
};
using Pkey = std::unique_ptr<EVP_PKEY, PkeyDeleter>;
using PkeyCtx = std::unique_ptr<EVP_PKEY_CTX, PkeyCtxDeleter>;
using CipherCtx = std::unique_ptr<EVP_CIPHER_CTX, CipherCtxDeleter>;

const unsigned char *Data(std::string_view s) {
	return reinterpret_cast<const unsigned char*>(s.data());
}

Pkey PrivateKey(const Bytes &raw) {
	if (raw.size() != kKeySize) {
		return nullptr;
	}
	return Pkey(EVP_PKEY_new_raw_private_key(
		EVP_PKEY_X25519, nullptr, raw.data(), raw.size()));
}

Pkey PublicKey(const Bytes &raw) {
	if (raw.size() != kKeySize) {
		return nullptr;
	}
	return Pkey(EVP_PKEY_new_raw_public_key(
		EVP_PKEY_X25519, nullptr, raw.data(), raw.size()));
}

Bytes RawPublic(EVP_PKEY *key) {
	auto out = Bytes(kKeySize);
	auto size = out.size();
	if (EVP_PKEY_get_raw_public_key(key, out.data(), &size) != 1 || size != kKeySize) {
		throw std::runtime_error("cannot export X25519 public key");
	}
	return out;
}

std::optional<Bytes> Exchange(EVP_PKEY *own, EVP_PKEY *peer) {
	const auto ctx = PkeyCtx(EVP_PKEY_CTX_new(own, nullptr));
	auto size = std::size_t(0);
	if (!ctx
		|| EVP_PKEY_derive_init(ctx.get()) != 1
		|| EVP_PKEY_derive_set_peer(ctx.get(), peer) != 1
		|| EVP_PKEY_derive(ctx.get(), nullptr, &size) != 1) {
		return std::nullopt;
	}
	auto out = Bytes(size);
	if (EVP_PKEY_derive(ctx.get(), out.data(), &size) != 1) {
		return std::nullopt;
	}
	out.resize(size);
	return out;
}

Bytes Hkdf(const Bytes &ikm, const Bytes &salt, std::string_view info) {
	const auto ctx = PkeyCtx(EVP_PKEY_CTX_new_id(EVP_PKEY_HKDF, nullptr));
	auto out = Bytes(kKeySize);
	auto size = out.size();
	if (!ctx
		|| EVP_PKEY_derive_init(ctx.get()) != 1
		|| EVP_PKEY_CTX_set_hkdf_md(ctx.get(), EVP_sha256()) != 1
		|| EVP_PKEY_CTX_set1_hkdf_salt(ctx.get(), salt.data(), int(salt.size())) != 1
		|| EVP_PKEY_CTX_set1_hkdf_key(ctx.get(), ikm.data(), int(ikm.size())) != 1
		|| EVP_PKEY_CTX_add1_hkdf_info(ctx.get(), Data(info), int(info.size())) != 1
		|| EVP_PKEY_derive(ctx.get(), out.data(), &size) != 1
		|| size != kKeySize) {
		throw std::runtime_error("HKDF failed");
	}
	return out;
}

Bytes Concat(const Bytes &a, const Bytes &b) {
	auto out = a;
	out.insert(out.end(), b.begin(), b.end());
	return out;
}

Bytes FromString(std::string_view s) {
	return Bytes(s.begin(), s.end());
}

// Returns ciphertext || tag.
Bytes GcmEncrypt(const Bytes &key, const Bytes &nonce, const Bytes &plaintext) {
	const auto ctx = CipherCtx(EVP_CIPHER_CTX_new());
	auto out = Bytes(plaintext.size() + kTagSize);
	auto len = 0;
	auto total = 0;
	if (!ctx
		|| EVP_EncryptInit_ex(ctx.get(), EVP_aes_256_gcm(), nullptr, nullptr, nullptr) != 1
		|| EVP_CIPHER_CTX_ctrl(ctx.get(), EVP_CTRL_GCM_SET_IVLEN, kNonceSize, nullptr) != 1
		|| EVP_EncryptInit_ex(ctx.get(), nullptr, nullptr, key.data(), nonce.data()) != 1
		|| EVP_EncryptUpdate(ctx.get(), nullptr, &len, Data(kAad), int(kAad.size())) != 1
		|| EVP_EncryptUpdate(ctx.get(), out.data(), &len, plaintext.data(), int(plaintext.size())) != 1) {
		throw std::runtime_error("AES-GCM encryption failed");
	}
	total = len;
	if (EVP_EncryptFinal_ex(ctx.get(), out.data() + total, &len) != 1
		|| EVP_CIPHER_CTX_ctrl(ctx.get(), EVP_CTRL_GCM_GET_TAG, kTagSize, out.data() + total + len) != 1) {
		throw std::runtime_error("AES-GCM encryption failed");
	}
	out.resize(total + len + kTagSize);
	return out;
}

std::optional<Bytes> GcmDecrypt(
		const Bytes &key,
		const std::uint8_t *nonce,
		const std::uint8_t *data,
		std::size_t size) {
	if (size < kTagSize) {
		return std::nullopt;
	}
	const auto cipherSize = int(size - kTagSize);
	const auto ctx = CipherCtx(EVP_CIPHER_CTX_new());
	auto out = Bytes(cipherSize);
	auto len = 0;
	auto tag = Bytes(data + cipherSize, data + size);
	if (!ctx
		|| EVP_DecryptInit_ex(ctx.get(), EVP_aes_256_gcm(), nullptr, nullptr, nullptr) != 1
		|| EVP_CIPHER_CTX_ctrl(ctx.get(), EVP_CTRL_GCM_SET_IVLEN, kNonceSize, nullptr) != 1
		|| EVP_DecryptInit_ex(ctx.get(), nullptr, nullptr, key.data(), nonce) != 1
		|| EVP_DecryptUpdate(ctx.get(), nullptr, &len, Data(kAad), int(kAad.size())) != 1
		|| EVP_DecryptUpdate(ctx.get(), out.data(), &len, data, cipherSize) != 1) {
		return std::nullopt;
	}
	const auto written = len;
	if (EVP_CIPHER_CTX_ctrl(ctx.get(), EVP_CTRL_GCM_SET_TAG, kTagSize, tag.data()) != 1
		|| EVP_DecryptFinal_ex(ctx.get(), out.data() + written, &len) != 1) {
		return std::nullopt;
	}
	out.resize(written + len);
	return out;
}

Bytes PairKey(const std::string &normalized) {
	return Hkdf(FromString(normalized), FromString(kPairSalt), kPairInfo);
}

} // namespace

KeyPair GenerateKeyPair() {
	// Any 32 random bytes are a valid X25519 private key (clamped on use).
	auto privateKey = RandomBytes(kKeySize);
	auto publicKey = PublicFromPrivate(privateKey);
	return KeyPair{ std::move(privateKey), std::move(publicKey) };
}

Bytes PublicFromPrivate(const Bytes &privateKey) {
	const auto key = PrivateKey(privateKey);
	return key ? RawPublic(key.get()) : Bytes();
}

Bytes Seal(const Bytes &ownerPublic, const Bytes &plaintext) {
	return SealWith(
		ownerPublic,
		plaintext,
		RandomBytes(kKeySize),
		RandomBytes(kNonceSize));
}

Bytes SealWith(
		const Bytes &ownerPublic,
		const Bytes &plaintext,
		const Bytes &ephemeralPrivate,
		const Bytes &nonce) {
	const auto ephemeral = PrivateKey(ephemeralPrivate);
	const auto owner = PublicKey(ownerPublic);
	if (!ephemeral || !owner || nonce.size() != kNonceSize) {
		throw std::invalid_argument("bad key or nonce");
	}
	const auto ephemeralPublic = RawPublic(ephemeral.get());
	const auto shared = Exchange(ephemeral.get(), owner.get());
	if (!shared) {
		throw std::runtime_error("X25519 failed");
	}
	const auto key = Hkdf(*shared, Concat(ephemeralPublic, ownerPublic), kEventInfo);
	auto out = Bytes{ kVersion };
	out.insert(out.end(), ephemeralPublic.begin(), ephemeralPublic.end());
	out.insert(out.end(), nonce.begin(), nonce.end());
	const auto sealed = GcmEncrypt(key, nonce, plaintext);
	out.insert(out.end(), sealed.begin(), sealed.end());
	return out;
}

std::optional<Bytes> Open(const Bytes &ownerPrivate, const Bytes &blob) {
	if (blob.size() < kHeaderSize + kTagSize || blob[0] != kVersion) {
		return std::nullopt;
	}
	const auto own = PrivateKey(ownerPrivate);
	const auto ephemeralPublic = Bytes(blob.begin() + 1, blob.begin() + 1 + kKeySize);
	const auto ephemeral = PublicKey(ephemeralPublic);
	if (!own || !ephemeral) {
		return std::nullopt;
	}
	const auto shared = Exchange(own.get(), ephemeral.get());
	if (!shared) {
		return std::nullopt;
	}
	const auto key = Hkdf(
		*shared,
		Concat(ephemeralPublic, RawPublic(own.get())),
		kEventInfo);
	return GcmDecrypt(
		key,
		blob.data() + 1 + kKeySize,
		blob.data() + kHeaderSize,
		blob.size() - kHeaderSize);
}

std::string NewPairCode() {
	auto result = std::string();
	const auto random = RandomBytes(kCodeLength);
	for (auto i = 0; i != kCodeLength; ++i) {
		if (i && !(i % 4)) {
			result += '-';
		}
		// 256 is a multiple of 32: no modulo bias.
		result += kCodeAlphabet[random[i] % kCodeAlphabet.size()];
	}
	return result;
}

std::optional<std::string> NormalizePairCode(std::string_view code) {
	auto result = std::string();
	for (auto ch : code) {
		if (ch == '-' || ch == ' ' || ch == '\t') {
			continue;
		}
		if (ch >= 'a' && ch <= 'z') {
			ch = char(ch - 'a' + 'A');
		}
		if (ch == 'O') {
			ch = '0';
		} else if (ch == 'I' || ch == 'L') {
			ch = '1';
		}
		if (kCodeAlphabet.find(ch) == std::string_view::npos) {
			return std::nullopt;
		}
		result += ch;
	}
	if (result.size() != kCodeLength) {
		return std::nullopt;
	}
	return result;
}

std::string PairLookupHash(const std::string &normalized) {
	return Sha256Hex(std::string(kPairLookupPrefix) + normalized);
}

Bytes PairEncrypt(const std::string &normalized, const Bytes &payload) {
	return PairEncryptWith(normalized, payload, RandomBytes(kNonceSize));
}

Bytes PairEncryptWith(
		const std::string &normalized,
		const Bytes &payload,
		const Bytes &nonce) {
	auto out = nonce;
	const auto sealed = GcmEncrypt(PairKey(normalized), nonce, payload);
	out.insert(out.end(), sealed.begin(), sealed.end());
	return out;
}

std::optional<Bytes> PairDecrypt(const std::string &normalized, const Bytes &data) {
	if (data.size() < kNonceSize + kTagSize) {
		return std::nullopt;
	}
	return GcmDecrypt(
		PairKey(normalized),
		data.data(),
		data.data() + kNonceSize,
		data.size() - kNonceSize);
}

std::string Sha256Hex(std::string_view data) {
	unsigned char digest[EVP_MAX_MD_SIZE];
	auto size = 0u;
	if (EVP_Digest(data.data(), data.size(), digest, &size, EVP_sha256(), nullptr) != 1) {
		throw std::runtime_error("SHA-256 failed");
	}
	return ToHex(Bytes(digest, digest + size));
}

Bytes RandomBytes(int count) {
	auto out = Bytes(count);
	if (count > 0 && RAND_bytes(out.data(), count) != 1) {
		throw std::runtime_error("RAND_bytes failed");
	}
	return out;
}

std::string ToHex(const Bytes &data) {
	static constexpr char kDigits[] = "0123456789abcdef";
	auto out = std::string();
	out.reserve(data.size() * 2);
	for (const auto byte : data) {
		out += kDigits[byte >> 4];
		out += kDigits[byte & 0x0F];
	}
	return out;
}

std::optional<Bytes> FromHex(std::string_view hex) {
	if (hex.size() % 2) {
		return std::nullopt;
	}
	const auto digit = [](char ch) -> int {
		if (ch >= '0' && ch <= '9') return ch - '0';
		if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
		if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
		return -1;
	};
	auto out = Bytes();
	out.reserve(hex.size() / 2);
	for (auto i = std::size_t(0); i != hex.size(); i += 2) {
		const auto high = digit(hex[i]);
		const auto low = digit(hex[i + 1]);
		if (high < 0 || low < 0) {
			return std::nullopt;
		}
		out.push_back(std::uint8_t(high * 16 + low));
	}
	return out;
}

} // namespace AyuSync::Crypto

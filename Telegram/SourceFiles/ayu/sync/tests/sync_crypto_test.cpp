// Checks ayu/sync/sync_crypto against docs/sync/test-vectors.json (flattened by
// flatten_vectors.py). Built and run by .github/workflows/sync.yml:
//   g++ -std=c++20 -I Telegram/SourceFiles sync_crypto_test.cpp sync_crypto.cpp -lcrypto
#include "ayu/sync/sync_crypto.h"

#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>

using namespace AyuSync::Crypto;

namespace {

int failures = 0;

void Check(bool ok, const std::string &what) {
	if (!ok) {
		++failures;
		std::cerr << "FAIL: " << what << "\n";
	}
}

Bytes Hex(const std::string &hex) {
	if (hex == "-") {
		return {};
	}
	const auto bytes = FromHex(hex);
	if (!bytes) {
		throw std::runtime_error("bad hex in vectors: " + hex);
	}
	return *bytes;
}

std::string DedupKey(
		const std::string &account,
		const std::string &dialog,
		const std::string &msgId,
		const std::string &editDate,
		const std::string &type) {
	return Sha256Hex(account + "|" + dialog + "|" + msgId + "|" + editDate + "|" + type);
}

void RoundTrips() {
	const auto pair = GenerateKeyPair();
	Check(pair.privateKey.size() == 32 && pair.publicKey.size() == 32, "key sizes");
	const auto text = Bytes{ 'h', 'e', 'l', 'l', 'o' };
	auto blob = Seal(pair.publicKey, text);
	Check(Open(pair.privateKey, blob) == text, "seal round trip");
	Check(Seal(pair.publicKey, text) != blob, "seal is randomized");
	const auto other = GenerateKeyPair();
	Check(!Open(other.privateKey, blob), "wrong key rejected");
	blob[50] ^= 1;
	Check(!Open(pair.privateKey, blob), "tampered blob rejected");
	Check(!Open(pair.privateKey, Bytes(40)), "short blob rejected");

	const auto code = NewPairCode();
	Check(code.size() == 14 && code[4] == '-' && code[9] == '-', "pair code format");
	const auto normalized = NormalizePairCode(code);
	Check(normalized.has_value(), "own code normalizes");
	Check(!NormalizePairCode("ABCD-EFGH"), "short code rejected");
	Check(!NormalizePairCode("ABCD-EFGH-JKM!"), "bad char rejected");
	const auto data = PairEncrypt(*normalized, text);
	Check(PairDecrypt(*normalized, data) == text, "pair round trip");
	Check(!PairDecrypt("000000000000", data), "wrong pair code rejected");
}

} // namespace

int main(int argc, char **argv) {
	if (argc != 2) {
		std::cerr << "usage: sync_crypto_test vectors.txt\n";
		return 2;
	}
	RoundTrips();

	auto input = std::ifstream(argv[1]);
	auto line = std::string();
	auto checked = 0;
	while (std::getline(input, line)) {
		auto in = std::istringstream(line);
		auto kind = std::string();
		in >> kind;
		if (kind == "pub") {
			std::string priv, pub;
			in >> priv >> pub;
			Check(ToHex(PublicFromPrivate(Hex(priv))) == pub, "public from private");
		} else if (kind == "seal") {
			std::string priv, pub, eph, nonce, plain, blob;
			in >> priv >> pub >> eph >> nonce >> plain >> blob;
			const auto sealed = SealWith(Hex(pub), Hex(plain), Hex(eph), Hex(nonce));
			Check(ToHex(sealed) == blob, "seal matches vector " + blob.substr(0, 16));
			const auto opened = Open(Hex(priv), Hex(blob));
			Check(opened && *opened == Hex(plain), "open vector " + blob.substr(0, 16));
		} else if (kind == "dedup") {
			std::string account, dialog, msgId, editDate, type, key;
			in >> account >> dialog >> msgId >> editDate >> type >> key;
			Check(DedupKey(account, dialog, msgId, editDate, type) == key, "dedup " + key);
		} else if (kind == "pair") {
			std::string code, normalized, lookup, nonce, payload, encrypted;
			in >> code >> normalized >> lookup >> nonce >> payload >> encrypted;
			Check(NormalizePairCode(code) == normalized, "normalize " + code);
			Check(PairLookupHash(normalized) == lookup, "pair lookup hash");
			Check(ToHex(PairEncryptWith(normalized, Hex(payload), Hex(nonce))) == encrypted,
				"pair encrypt");
			Check(PairDecrypt(normalized, Hex(encrypted)) == Hex(payload), "pair decrypt");
		} else if (kind == "normalize") {
			std::string typed, normalized;
			in >> typed >> normalized;
			for (auto &ch : typed) {
				if (ch == '_') {
					ch = ' ';
				}
			}
			Check(NormalizePairCode(typed) == normalized, "normalize typed code");
		} else {
			continue;
		}
		++checked;
	}
	Check(checked >= 10, "vectors were read");
	std::cout << checked << " vector lines checked, " << failures << " failures\n";
	return failures ? 1 : 0;
}

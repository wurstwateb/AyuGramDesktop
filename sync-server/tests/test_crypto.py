import pytest

from typsync import crypto


def test_seal_round_trip():
    priv, pub = crypto.generate_keypair()
    blob = crypto.seal(pub, "привет 👋".encode())
    assert blob[0] == crypto.VERSION
    assert crypto.open_sealed(priv, blob) == "привет 👋".encode()


def test_seal_is_randomized():
    _, pub = crypto.generate_keypair()
    assert crypto.seal(pub, b"x") != crypto.seal(pub, b"x")


def test_public_from_private():
    priv, pub = crypto.generate_keypair()
    assert crypto.public_from_private(priv) == pub


def test_wrong_key_fails():
    _, pub = crypto.generate_keypair()
    other_priv, _ = crypto.generate_keypair()
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(other_priv, crypto.seal(pub, b"secret"))


@pytest.mark.parametrize("index", [0, 5, 40, 50, -1])
def test_tampered_byte_fails(index):
    priv, pub = crypto.generate_keypair()
    blob = bytearray(crypto.seal(pub, b"secret message"))
    blob[index] ^= 0x01
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(priv, bytes(blob))


def test_truncated_fails():
    priv, pub = crypto.generate_keypair()
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(priv, crypto.seal(pub, b"abc")[:40])


def test_pair_code_format():
    code = crypto.new_pair_code()
    assert len(code) == 14 and code[4] == code[9] == "-"
    assert crypto.normalize_pair_code(code) == code.replace("-", "")


def test_pair_code_normalization():
    assert crypto.normalize_pair_code("abcd-efgh-o1il") == "ABCDEFGH0111"
    assert crypto.pair_lookup_hash("abcd efgh 0111") == crypto.pair_lookup_hash("ABCD-EFGH-O1IL")


@pytest.mark.parametrize("code", ["ABCD-EFGH", "ABCD-EFGH-JKMN-P", "ABCD-EFGH-JKM!"])
def test_pair_code_invalid(code):
    with pytest.raises(crypto.CryptoError):
        crypto.normalize_pair_code(code)


def test_pair_round_trip_and_wrong_code():
    code = crypto.new_pair_code()
    data = crypto.pair_encrypt(code, b"owner private key")
    assert crypto.pair_decrypt(code.lower(), data) == b"owner private key"
    other = "0000-0000-0000" if code != "0000-0000-0000" else "1111-1111-1111"
    with pytest.raises(crypto.CryptoError):
        crypto.pair_decrypt(other, data)

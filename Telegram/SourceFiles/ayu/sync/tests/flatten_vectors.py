"""Turns docs/sync/test-vectors.json into whitespace-separated lines that the
C++ test can read without a JSON library.

    python flatten_vectors.py docs/sync/test-vectors.json > vectors.txt
"""
import json
import sys


def main(path: str) -> None:
    v = json.load(open(path, encoding="utf-8"))
    print("pub", v["ownerPrivateHex"], v["ownerPublicHex"])
    for item in v["seal"]:
        print("seal", v["ownerPrivateHex"], v["ownerPublicHex"], item["ephemeralPrivateHex"],
              item["nonceHex"], item["plaintextHex"] or "-", item["blobHex"])
        e = item["event"]
        if e["type"] == "message":
            s = e["snapshot"]
            rows = [(s["dialog"], s["msgId"], s["editDate"])]
        elif e["type"] == "edit":
            p = e["previous"]
            rows = [(p["dialog"], p["msgId"], e["newEditDate"])]
        else:
            rows = [(e["dialog"], i, 0) for i in sorted(set(e["msgIds"]))]
        for (dialog, msg_id, edit_date), key in zip(rows, item["dedupKeys"]):
            print("dedup", e["account"], dialog, msg_id, edit_date, e["type"], key)
    p = v["pair"]
    print("pair", p["code"], p["normalized"], p["lookupHash"], p["nonceHex"],
          p["payloadHex"], p["encryptedHex"])
    print("normalize", p["codeAsTyped"].replace(" ", "_"), p["normalized"])


if __name__ == "__main__":
    main(sys.argv[1])

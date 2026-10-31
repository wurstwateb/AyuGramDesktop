// This is the source code of AyuGram for Desktop.
//
// We do not and cannot prevent the use of our code,
// but be respectful and credit the original author.
//
// Copyright @Radolyn, 2026
#pragma once

#include <string>

using ID = long long;

class AyuMessageBase
{
public:
	ID fakeId = 0;
	ID userId = 0;
	ID dialogId = 0;
	ID groupedId = 0;
	ID peerId = 0;
	ID fromId = 0;
	ID topicId = 0;
	int messageId = 0;
	int date = 0;
	int flags = 0;
	int editDate = 0;
	int views = 0;
	int fwdFlags = 0;
	ID fwdFromId = 0;
	std::string fwdName;
	int fwdDate = 0;
	std::string fwdPostAuthor;
	std::string postAuthor;
	int replyFlags = 0;
	int replyMessageId = 0;
	ID replyPeerId = 0;
	int replyTopId = 0;
	bool replyForumTopic = false;
	std::vector<char> replySerialized;
	std::vector<char> replyMarkupSerialized;
	int entityCreateDate = 0;
	std::string text;
	std::vector<char> textEntities;
	std::string mediaPath;
	std::string hqThumbPath;
	int documentType = 0;
	std::vector<char> documentSerialized;
	std::vector<char> thumbsSerialized;
	std::vector<char> documentAttributesSerialized;
	std::string mimeType;
};

class DeletedMessage : public AyuMessageBase
{
};

class EditedMessage : public AyuMessageBase
{
};

class DeletedDialog
{
public:
	ID fakeId = 0;
	ID userId = 0;
	ID dialogId = 0;
	ID peerId = 0;
	std::unique_ptr<int> folderId; // nullable
	int topMessage = 0;
	int lastMessageDate = 0;
	int flags = 0;
	int entityCreateDate = 0;
};

class RegexFilter
{
public:
	std::vector<char> id;
	std::string text;
	bool enabled = false;
	bool reversed = false;
	bool caseInsensitive = false;
	std::optional<ID> dialogId; // nullable

	bool operator==(const RegexFilter &other) const {
		return id == other.id &&
			text == other.text &&
			caseInsensitive == other.caseInsensitive &&
			reversed == other.reversed &&
			dialogId == other.dialogId &&
			enabled == other.enabled;
	}
	[[nodiscard]] QJsonObject toJson() const {
		QJsonObject json;
		json["id"] = QString::fromUtf8(id.data());
		json["text"] = QString::fromStdString(text);
		json["enabled"] = enabled;
		json["reversed"] = reversed;
		json["caseInsensitive"] = caseInsensitive;
		if (dialogId.has_value()) {
			json["dialogId"] = dialogId.value();
		}
		return json;
	}
};

class RegexFilterGlobalExclusion
{
public:
	ID fakeId = 0;
	ID dialogId = 0;
	std::vector<char> filterId;

	bool operator==(const RegexFilterGlobalExclusion& other) const {
		return dialogId == other.dialogId && filterId == other.filterId;
	}
};

class SpyMessageRead
{
public:
	ID fakeId = 0;
	ID userId = 0;
	ID dialogId = 0;
	int messageId = 0;
	int entityCreateDate = 0;
};

class SpyMessageContentsRead
{
public:
	ID fakeId = 0;
	ID userId = 0;
	ID dialogId = 0;
	int messageId = 0;
	int entityCreateDate = 0;
};

// TypendruckerGram Sync: every message the client received, kept forever, so
// a deletion can be restored even when the chat was never opened.
class CachedMessage
{
public:
	ID userId = 0;
	ID dialogId = 0;
	int messageId = 0;
	bool channel = false;
	ID topicId = 0;
	ID fromId = 0;
	ID groupedId = 0;
	int date = 0;
	int editDate = 0;
	int replyMessageId = 0;
	std::string text;
	std::string entitiesJson;
	std::string mediaKind;
	std::string mediaPath;
	std::string mediaMime;
	std::string mediaName;
	long long mediaSize = 0;
};

// Sealed events waiting to be sent to the relay.
class SyncOutboxItem
{
public:
	ID id = 0;
	ID userId = 0;
	std::string relayKey;
	std::vector<char> blob;
	std::string mediaPath; // plaintext file to upload as a blob first, may be empty
	std::string blobId;
	int createdAt = 0;
	int attempts = 0;
};

// Dedup keys of events already applied on this device.
class SyncSeenKey
{
public:
	ID userId = 0;
	std::string dedupKey;
	int appliedAt = 0;
};

class SyncState
{
public:
	ID userId = 0;
	bool enabled = false;
	std::string relayUrl;
	std::string ownerId;
	std::string deviceId;
	std::string token;
	std::vector<char> ownerPrivate;
	std::vector<char> ownerPublic;
	long long cursor = 0;
	long long mediaLimit = 20 * 1024 * 1024;
	int lastSyncAt = 0;
	bool trimmed = false;
};

// This is the source code of AyuGram for Desktop.
//
// We do not and cannot prevent the use of our code,
// but be respectful and credit the original author.
//
// Copyright @Radolyn, 2026
#pragma once

#include "ayu/data/entities.h"

#include <functional>
#include <optional>

class SchemaVersion
{
public:
	int id;
	int version;
};

namespace AyuDatabase {

void initialize();

void addEditedMessage(const EditedMessage &message);
std::vector<EditedMessage> getEditedMessages(ID userId, ID dialogId, ID messageId, ID minId, ID maxId, int totalLimit);
bool hasRevisions(ID userId, ID dialogId, ID messageId);

void addDeletedMessage(const DeletedMessage &message);
std::vector<DeletedMessage> getDeletedMessages(ID userId, ID dialogId, ID topicId, ID minId, ID maxId, int totalLimit, const std::string &searchQuery = "");
std::vector<DeletedMessage> getDeletedMessagesByDate(ID userId, ID dialogId, int minDate, int maxDate);
bool hasDeletedMessages(ID userId, ID dialogId, ID topicId);
void clearDeletedMessages(ID userId, ID dialogId, ID topicId);

std::vector<RegexFilter> getAllRegexFilters();
RegexFilter getById(std::vector<char> id);
std::vector<RegexFilter> getShared();
std::vector<RegexFilter> getByDialogId(ID dialogId);
std::vector<RegexFilterGlobalExclusion> getAllFiltersExclusions();
std::vector<RegexFilter> getExcludedByDialogId(ID dialogId);

int getCount();


void addRegexFilter(const RegexFilter &filter);
void addRegexExclusion(const RegexFilterGlobalExclusion &exclusion);

void updateRegexFilter(const RegexFilter &filter);

void deleteFilter(const std::vector<char> &id);
void deleteExclusionsByFilterId(const std::vector<char> &id);
void deleteExclusion(ID dialogId, std::vector<char> filterId);

void deleteAllFilters();
void deleteAllExclusions();

bool hasFilters();
bool hasPerDialogFilters();

void moveCurrentDatabase();

// TypendruckerGram Sync.
void upsertCachedMessages(const std::vector<CachedMessage> &messages);
std::optional<CachedMessage> getCachedMessage(ID userId, ID dialogId, int messageId);
// Private chats and basic groups: message ids are unique per account.
std::optional<CachedMessage> findNonChannelCachedMessage(ID userId, int messageId);

void addOutbox(const SyncOutboxItem &item);
std::vector<SyncOutboxItem> getOutbox(ID userId, int limit);
void removeOutbox(const std::vector<ID> &ids);
void updateOutbox(const SyncOutboxItem &item);

// Returns false if the key was already applied.
bool markSeen(ID userId, const std::string &dedupKey, int now);

std::optional<SyncState> getSyncState(ID userId);
void saveSyncState(const SyncState &state);

}

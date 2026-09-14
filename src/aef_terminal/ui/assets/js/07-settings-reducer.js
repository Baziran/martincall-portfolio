    const SERVER_SETTING_WRITER_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

    function normalizeServerSettingMutationRecord(record, options = {}) {
      const includeValue = options.includeValue === true;
      const expectedKeys = includeValue
        ? "changed_at_ms,key,sequence,value,writer_id"
        : "changed_at_ms,key,sequence,writer_id";
      if (
        !record
        || typeof record !== "object"
        || Array.isArray(record)
        || Object.keys(record).sort().join(",") !== expectedKeys
      ) throw new TypeError("SETTINGS_MUTATION_RECORD_INVALID");
      const key = record.key;
      const changedAtMs = record.changed_at_ms;
      const sequence = record.sequence;
      const writerId = record.writer_id;
      const internalOrder = writerId === null && changedAtMs === 0 && sequence === 0;
      const browserOrder = SERVER_SETTING_WRITER_ID_PATTERN.test(String(writerId || ""))
        && Number.isSafeInteger(changedAtMs)
        && changedAtMs > 0
        && Number.isSafeInteger(sequence)
        && sequence > 0;
      if (
        typeof key !== "string"
        || !key
        || (includeValue && typeof record.value !== "string")
        || (!internalOrder && !browserOrder)
      ) {
        throw new TypeError("SETTINGS_MUTATION_RECORD_INVALID");
      }
      return {
        key,
        ...(includeValue ? { value: record.value } : {}),
        changed_at_ms: changedAtMs,
        writer_id: writerId,
        sequence,
      };
    }

    function serverSettingMutationOrderIsNewer(candidate, current) {
      if (!current) return true;
      if (candidate.changed_at_ms !== current.changed_at_ms) {
        return candidate.changed_at_ms > current.changed_at_ms;
      }
      const candidateWriter = String(candidate.writer_id || "");
      const currentWriter = String(current.writer_id || "");
      if (candidateWriter !== currentWriter) return candidateWriter > currentWriter;
      return candidate.sequence > current.sequence;
    }

    function reduceServerSettingReconciliation(input) {
      const records = input?.records;
      const settings = input?.settings;
      const authoritativeOrders = input?.authoritativeOrders;
      const latestMutations = input?.latestMutations;
      const queuedKeys = input?.queuedKeys;
      const serverSettingKeys = input?.serverSettingKeys;
      const settingsRevision = input?.settingsRevision;
      const writerId = input?.writerId;
      const snapshotKeys = input?.snapshotKeys;
      const invalidCode = String(input?.invalidCode || "SETTINGS_MUTATION_RECONCILIATION_INVALID");
      if (
        !Array.isArray(records)
        || !settings
        || typeof settings !== "object"
        || !(authoritativeOrders instanceof Map)
        || !(latestMutations instanceof Map)
        || !(queuedKeys instanceof Set)
        || !(serverSettingKeys instanceof Set)
        || !Number.isSafeInteger(settingsRevision)
        || settingsRevision < 0
        || (snapshotKeys !== null && snapshotKeys !== undefined && !(snapshotKeys instanceof Set))
      ) throw new TypeError(invalidCode);

      const authoritativeRecords = [];
      const appliedRecords = [];
      const deletedAuthoritativeKeys = new Set();
      const deletedSettingKeys = [];
      let changed = false;

      for (const record of records) {
        const currentAuthoritative = authoritativeOrders.get(record.key);
        const internalRecord = record.writer_id === null;
        const recordIsNewer = !currentAuthoritative
          || (internalRecord
            ? settingsRevision > Number(currentAuthoritative.settings_revision || 0)
            : serverSettingMutationOrderIsNewer(record, currentAuthoritative));
        const currentIsNewer = Boolean(currentAuthoritative) && (internalRecord
          ? Number(currentAuthoritative.settings_revision || 0) > settingsRevision
          : serverSettingMutationOrderIsNewer(currentAuthoritative, record));
        if (
          currentAuthoritative
          && !recordIsNewer
          && !currentIsNewer
          && JSON.stringify(currentAuthoritative.value) !== JSON.stringify(record.value)
        ) throw new TypeError(invalidCode);
        if (currentIsNewer) continue;
        if (recordIsNewer || !currentAuthoritative) {
          authoritativeRecords.push({
            ...record,
            settings_revision: settingsRevision,
          });
        } else if (settingsRevision > Number(currentAuthoritative.settings_revision || 0)) {
          authoritativeRecords.push({
            ...currentAuthoritative,
            settings_revision: settingsRevision,
          });
        }
        if (typeof record.value !== "string") continue;
        const latest = latestMutations.get(record.key);
        if (latest) {
          const latestOrder = {
            changed_at_ms: latest.changed_at_ms,
            writer_id: writerId,
            sequence: latest.sequence,
          };
          if (!serverSettingMutationOrderIsNewer(record, latestOrder)) continue;
        }
        appliedRecords.push(record);
        if (settings[record.key] !== record.value) changed = true;
      }

      if (snapshotKeys instanceof Set) {
        for (const key of serverSettingKeys) {
          if (
            snapshotKeys.has(key)
            || queuedKeys.has(key)
            || latestMutations.has(key)
          ) continue;
          deletedSettingKeys.push(key);
          deletedAuthoritativeKeys.add(key);
          changed = true;
        }
        for (const key of authoritativeOrders.keys()) {
          if (
            snapshotKeys.has(key)
            || queuedKeys.has(key)
            || latestMutations.has(key)
          ) continue;
          deletedAuthoritativeKeys.add(key);
        }
      }

      return {
        changed,
        authoritativeRecords,
        appliedRecords,
        deletedAuthoritativeKeys: Array.from(deletedAuthoritativeKeys),
        deletedSettingKeys,
      };
    }

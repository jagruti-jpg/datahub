-- 1. SESSIONS TABLE
CREATE TABLE IF NOT EXISTS sessions (
                                        id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    system_prompt TEXT,
    status ENUM('active', 'completed', 'failed', 'paused') DEFAULT 'active',
    summary TEXT DEFAULT NULL,          -- rolling summary of messages older than the keep-window (6 msgs)
    metadata JSON NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_user_id (user_id)
    );

-- 2. MESSAGES TABLE
CREATE TABLE IF NOT EXISTS messages (
                                        id VARCHAR(36) PRIMARY KEY,
    session_id VARCHAR(36) NOT NULL,
    role ENUM('user', 'assistant', 'system') NOT NULL,
    content LONGTEXT NOT NULL,
    tokens_used INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    INDEX idx_session_id (session_id)
    );

-- 3. SKILLS TABLE
-- A "skill" is a named system prompt the chat widget can switch to. The built-in
-- "Default" row holds the baseline prompt and is protected from deletion in the API.
CREATE TABLE IF NOT EXISTS skills (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    skill TEXT NOT NULL,
    is_default TINYINT(1) NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- 4. PII VERDICTS LEDGER
-- One row per (dataset, column, label) the classifier has ruled on. This is what makes
-- unattended tagging safe to run: 'rejected' rows suppress a label a steward already
-- turned down, 'pending' rows outlive process restarts until someone reviews them, and
-- 'would_apply' rows are the sample the auto-apply floor is calibrated against.
-- Deliberately no surrogate uniqueness column across (dataset_urn, field_path, label):
-- such an index would exceed InnoDB's 3072-byte key limit at utf8mb4's 4 bytes per char,
-- and a hashed stand-in could not be added to installs that already have this table --
-- init_db() runs CREATE TABLE IF NOT EXISTS, which never alters an existing one.
-- Deduplication is done in pii_store against the idx_field index below.
CREATE TABLE IF NOT EXISTS pii_verdicts (
    id VARCHAR(36) PRIMARY KEY,
    dataset_urn VARCHAR(512) NOT NULL,
    dataset_name VARCHAR(255) NOT NULL,
    field_path VARCHAR(512) NOT NULL,
    label VARCHAR(64) NOT NULL,
    confidence DECIMAL(4,3) NOT NULL,
    source ENUM('rule','model') NOT NULL,
    reason TEXT,
    tier ENUM('auto','review','weak') NOT NULL,
    status ENUM('applied','pending','rejected','skipped','would_apply','reverted','failed') NOT NULL,
    -- Structural only: field_path:native_type, never tags. Hashing tags here would let
    -- our own write invalidate the fingerprint on the very next run.
    schema_fingerprint VARCHAR(32) NOT NULL,
    created_by VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP NULL,
    resolved_by VARCHAR(255) NULL,
    -- Prefixed at 191 chars because utf8mb4 caps a single index part at 767 bytes.
    INDEX idx_queue (status, dataset_urn(191)),
    INDEX idx_field (dataset_urn(191), field_path(191), label)
);

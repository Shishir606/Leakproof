-- Frozen schema before multi-resource foundation. Disposable upgrade tests only.
CREATE TYPE leak_type AS ENUM ('PAYMENT_FAILURE', 'CHECKOUT_ABANDON', 'SUBSCRIPTION_HALT', 'INVOICE_OVERDUE');
CREATE TYPE case_state AS ENUM ('DETECTED', 'DIAGNOSED', 'PLANNED', 'ACTING', 'WAITING', 'VERIFYING', 'CLOSED', 'SUPPRESSED', 'STOPPED', 'ESCALATED');
CREATE TYPE arm AS ENUM ('TREATMENT', 'HOLDOUT');
CREATE TYPE case_outcome AS ENUM ('RECOVERED', 'LOST', 'ABANDONED', 'HUMAN', 'SUPPRESSED');
CREATE TYPE demo_session_state AS ENUM ('CREATED', 'CHECKOUT_OPEN', 'AT_RISK', 'RECOVERED', 'EXPIRED');

CREATE TABLE merchants (
	id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	policy JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE suppressions (
	id BIGSERIAL NOT NULL,
	merchant_id VARCHAR NOT NULL,
	scope JSONB NOT NULL,
	pattern VARCHAR NOT NULL,
	reason TEXT NOT NULL,
	opened_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	opened_by VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_suppressions_merchant_expires ON suppressions (merchant_id, expires_at);

CREATE TABLE eval_runs (
	id BIGSERIAL NOT NULL,
	suite VARCHAR NOT NULL,
	prompt_version VARCHAR,
	model VARCHAR,
	metrics JSONB NOT NULL,
	passed BOOLEAN NOT NULL,
	ran_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE webhook_events (
	id BIGSERIAL NOT NULL,
	merchant_id VARCHAR NOT NULL,
	provider VARCHAR NOT NULL,
	provider_event_key VARCHAR NOT NULL,
	event_type VARCHAR NOT NULL,
	payload JSONB NOT NULL,
	signature_verified BOOLEAN NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	processed_at TIMESTAMP WITH TIME ZONE,
	processing_attempts INTEGER NOT NULL,
	last_error TEXT,
	PRIMARY KEY (id),
	CONSTRAINT uq_webhook_provider_event UNIQUE (merchant_id, provider, provider_event_key)
)

;
CREATE INDEX ix_webhooks_unprocessed ON webhook_events (processed_at, received_at);

CREATE TABLE email_delivery_events (
	id BIGSERIAL NOT NULL,
	provider_email_id VARCHAR(128) NOT NULL,
	provider_event_id VARCHAR(128) NOT NULL,
	event_type VARCHAR(40) NOT NULL,
	safe_payload JSONB NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_email_delivery_provider_event UNIQUE (provider_email_id, provider_event_id)
)

;
CREATE INDEX ix_email_delivery_events_email_created ON email_delivery_events (provider_email_id, created_at);

CREATE TABLE customers (
	id VARCHAR NOT NULL,
	merchant_id VARCHAR NOT NULL,
	segment VARCHAR,
	locale VARCHAR NOT NULL,
	protected BOOLEAN NOT NULL,
	dnc BOOLEAN NOT NULL,
	dnc_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(merchant_id) REFERENCES merchants (id)
)

;

CREATE TABLE batch_runs (
	id VARCHAR NOT NULL,
	merchant_id VARCHAR NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	holdout_seed INTEGER NOT NULL,
	holdout_fraction NUMERIC(5, 4) NOT NULL,
	measurement_config JSONB NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(merchant_id) REFERENCES merchants (id)
)

;

CREATE TABLE payment_attempt_observations (
	id BIGSERIAL NOT NULL,
	merchant_id VARCHAR NOT NULL,
	provider VARCHAR(40) NOT NULL,
	namespace VARCHAR(160) NOT NULL,
	attempt_key VARCHAR(255) NOT NULL,
	provider_event_key VARCHAR(255) NOT NULL,
	provider_payment_id VARCHAR(255),
	provider_order_id VARCHAR(255),
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	outcome VARCHAR(16) NOT NULL,
	method VARCHAR(80) NOT NULL,
	issuer VARCHAR(120) NOT NULL,
	bin_bucket VARCHAR(32) NOT NULL,
	checkout_step VARCHAR(120) NOT NULL,
	checkout_version VARCHAR(80) NOT NULL,
	error_reason VARCHAR(160) NOT NULL,
	source VARCHAR(40) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_payment_attempt_provider_key UNIQUE (merchant_id, provider, namespace, attempt_key),
	FOREIGN KEY(merchant_id) REFERENCES merchants (id)
)

;
CREATE INDEX ix_payment_attempt_outcome ON payment_attempt_observations (merchant_id, namespace, outcome);
CREATE INDEX ix_payment_attempt_method ON payment_attempt_observations (merchant_id, namespace, method);
CREATE INDEX ix_payment_attempt_issuer ON payment_attempt_observations (merchant_id, namespace, issuer);
CREATE INDEX ix_payment_attempt_merchant_observed ON payment_attempt_observations (merchant_id, namespace, observed_at);

CREATE TABLE consents (
	id BIGSERIAL NOT NULL,
	customer_id VARCHAR NOT NULL,
	channel VARCHAR NOT NULL,
	granted BOOLEAN NOT NULL,
	basis VARCHAR NOT NULL,
	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (customer_id, channel),
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)

;

CREATE TABLE cases (
	id VARCHAR NOT NULL,
	merchant_id VARCHAR NOT NULL,
	customer_id VARCHAR NOT NULL,
	leak_type leak_type NOT NULL,
	entity_type VARCHAR NOT NULL,
	entity_id VARCHAR NOT NULL,
	dedupe_key VARCHAR NOT NULL,
	batch_run_id VARCHAR,
	amount_band VARCHAR NOT NULL,
	amount_at_risk BIGINT NOT NULL,
	currency VARCHAR NOT NULL,
	state case_state NOT NULL,
	arm arm NOT NULL,
	outcome case_outcome,
	detected_at TIMESTAMP WITH TIME ZONE NOT NULL,
	closed_at TIMESTAMP WITH TIME ZONE,
	attribution_until TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_cases_merchant_dedupe UNIQUE (merchant_id, dedupe_key),
	FOREIGN KEY(merchant_id) REFERENCES merchants (id),
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)

;
CREATE INDEX ix_cases_customer ON cases (customer_id);
CREATE INDEX ix_cases_batch_run ON cases (batch_run_id);
CREATE INDEX ix_cases_merchant_state ON cases (merchant_id, state);

CREATE TABLE demo_sessions (
	id VARCHAR NOT NULL,
	merchant_id VARCHAR NOT NULL,
	customer_id VARCHAR NOT NULL,
	razorpay_order_id VARCHAR NOT NULL,
	amount_paise BIGINT NOT NULL,
	currency VARCHAR(3) NOT NULL,
	state demo_session_state NOT NULL,
	recipient_ciphertext TEXT,
	recipient_hash VARCHAR(64),
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_demo_sessions_razorpay_order UNIQUE (razorpay_order_id),
	FOREIGN KEY(merchant_id) REFERENCES merchants (id),
	FOREIGN KEY(customer_id) REFERENCES customers (id)
)

;
CREATE INDEX ix_demo_sessions_recipient_hash ON demo_sessions (recipient_hash);
CREATE INDEX ix_demo_sessions_expires ON demo_sessions (expires_at);
CREATE INDEX ix_demo_sessions_merchant_state ON demo_sessions (merchant_id, state);

CREATE TABLE events (
	id BIGSERIAL NOT NULL,
	case_id VARCHAR NOT NULL,
	seq INTEGER NOT NULL,
	kind VARCHAR NOT NULL,
	payload JSONB NOT NULL,
	actor VARCHAR NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_events_case_seq UNIQUE (case_id, seq),
	FOREIGN KEY(case_id) REFERENCES cases (id)
)

;

CREATE TABLE diagnoses (
	case_id VARCHAR NOT NULL,
	tier SMALLINT NOT NULL,
	failure_class VARCHAR NOT NULL,
	confidence NUMERIC(4, 3) NOT NULL,
	evidence JSONB NOT NULL,
	rule_id VARCHAR,
	diagnosed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (case_id),
	FOREIGN KEY(case_id) REFERENCES cases (id)
)

;

CREATE TABLE actions (
	id VARCHAR NOT NULL,
	case_id VARCHAR NOT NULL,
	step_index SMALLINT NOT NULL,
	action_type VARCHAR NOT NULL,
	scheduled_for TIMESTAMP WITH TIME ZONE NOT NULL,
	verdict VARCHAR,
	verdict_rules JSONB,
	executed_at TIMESTAMP WITH TIME ZONE,
	idempotency_key VARCHAR,
	provider_ref VARCHAR,
	status VARCHAR,
	attempt_count INTEGER NOT NULL,
	cost_paise BIGINT NOT NULL,
	ev_estimate BIGINT,
	PRIMARY KEY (id),
	CONSTRAINT uq_actions_case_step UNIQUE (case_id, step_index),
	CONSTRAINT uq_actions_idempotency_key UNIQUE (idempotency_key),
	FOREIGN KEY(case_id) REFERENCES cases (id)
)

;

CREATE TABLE contacts (
	id BIGSERIAL NOT NULL,
	customer_id VARCHAR NOT NULL,
	channel VARCHAR NOT NULL,
	case_id VARCHAR NOT NULL,
	sent_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(customer_id) REFERENCES customers (id),
	FOREIGN KEY(case_id) REFERENCES cases (id)
)

;
CREATE INDEX ix_contacts_customer_sent ON contacts (customer_id, sent_at);

CREATE TABLE promises (
	id BIGSERIAL NOT NULL,
	case_id VARCHAR NOT NULL,
	promised_on DATE NOT NULL,
	amount_paise BIGINT NOT NULL,
	captured_via VARCHAR NOT NULL,
	kept BOOLEAN,
	transcript_ref VARCHAR,
	PRIMARY KEY (id),
	CONSTRAINT uq_promises_case_transcript UNIQUE (case_id, transcript_ref),
	FOREIGN KEY(case_id) REFERENCES cases (id)
)

;

CREATE TABLE llm_calls (
	id BIGSERIAL NOT NULL,
	merchant_id VARCHAR,
	case_id VARCHAR,
	batch_run_id VARCHAR,
	purpose VARCHAR NOT NULL,
	provider VARCHAR(40) NOT NULL,
	request_id VARCHAR(255),
	error_class VARCHAR(100),
	model VARCHAR NOT NULL,
	prompt_version VARCHAR NOT NULL,
	input_tokens INTEGER NOT NULL,
	output_tokens INTEGER NOT NULL,
	cost_paise BIGINT NOT NULL,
	latency_ms INTEGER NOT NULL,
	schema_ok BOOLEAN NOT NULL,
	retries SMALLINT NOT NULL,
	called_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(merchant_id) REFERENCES merchants (id),
	FOREIGN KEY(case_id) REFERENCES cases (id),
	FOREIGN KEY(batch_run_id) REFERENCES batch_runs (id)
)

;

CREATE TABLE checkout_events (
	id BIGSERIAL NOT NULL,
	session_id VARCHAR NOT NULL,
	client_event_id VARCHAR(128) NOT NULL,
	event_type VARCHAR(40) NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	metadata JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_checkout_events_session_client UNIQUE (session_id, client_event_id),
	FOREIGN KEY(session_id) REFERENCES demo_sessions (id)
)

;
CREATE INDEX ix_checkout_events_session_received ON checkout_events (session_id, received_at);

CREATE TABLE case_insights (
	case_id VARCHAR NOT NULL,
	summary VARCHAR(500),
	probable_cause VARCHAR(500),
	evidence JSONB NOT NULL,
	recommended_next_step VARCHAR(500),
	confidence NUMERIC(4, 3),
	status VARCHAR(40) NOT NULL,
	fallback_reason VARCHAR(100),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (case_id),
	CONSTRAINT ck_case_insight_confidence CHECK (confidence >= 0 AND confidence <= 1),
	FOREIGN KEY(case_id) REFERENCES cases (id)
)

;

CREATE TABLE actuator_receipts (
	idempotency_key VARCHAR NOT NULL,
	action_id VARCHAR NOT NULL,
	provider VARCHAR NOT NULL,
	provider_ref VARCHAR NOT NULL,
	request JSONB NOT NULL,
	response JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (idempotency_key),
	FOREIGN KEY(action_id) REFERENCES actions (id)
)

;

CREATE TABLE recovery_attributions (
	id BIGSERIAL NOT NULL,
	case_id VARCHAR NOT NULL,
	payment_entity_id VARCHAR NOT NULL,
	amount_paise BIGINT NOT NULL,
	matched_by VARCHAR NOT NULL,
	credit_rule VARCHAR NOT NULL,
	credited_action_id VARCHAR,
	credited_action_type VARCHAR,
	touch_at TIMESTAMP WITH TIME ZONE,
	organic BOOLEAN NOT NULL,
	paid_at TIMESTAMP WITH TIME ZONE NOT NULL,
	attributed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_attribution_case UNIQUE (case_id),
	FOREIGN KEY(case_id) REFERENCES cases (id),
	FOREIGN KEY(credited_action_id) REFERENCES actions (id)
)

;

CREATE TABLE voice_turns (
	provider_turn_id VARCHAR NOT NULL,
	action_id VARCHAR NOT NULL,
	case_id VARCHAR NOT NULL,
	turn_number SMALLINT NOT NULL,
	transcript TEXT NOT NULL,
	intent VARCHAR NOT NULL,
	reply_template_id VARCHAR NOT NULL,
	ended BOOLEAN NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (provider_turn_id),
	CONSTRAINT uq_voice_turn_action_number UNIQUE (action_id, turn_number),
	FOREIGN KEY(action_id) REFERENCES actions (id),
	FOREIGN KEY(case_id) REFERENCES cases (id)
)

;

CREATE TABLE provider_calls (
	id BIGSERIAL NOT NULL,
	session_id VARCHAR,
	case_id VARCHAR,
	action_id VARCHAR,
	provider VARCHAR(40) NOT NULL,
	operation VARCHAR(80) NOT NULL,
	request_id VARCHAR(255),
	safe_response_metadata JSONB NOT NULL,
	latency_ms INTEGER NOT NULL,
	attempt_number SMALLINT NOT NULL,
	status VARCHAR(40) NOT NULL,
	error_class VARCHAR(100),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(session_id) REFERENCES demo_sessions (id),
	FOREIGN KEY(case_id) REFERENCES cases (id),
	FOREIGN KEY(action_id) REFERENCES actions (id)
)

;
CREATE INDEX ix_provider_calls_provider_status ON provider_calls (provider, status);
CREATE INDEX ix_provider_calls_case ON provider_calls (case_id, created_at);
CREATE INDEX ix_provider_calls_session ON provider_calls (session_id, created_at);

CREATE TABLE email_deliveries (
	id BIGSERIAL NOT NULL,
	session_id VARCHAR NOT NULL,
	case_id VARCHAR NOT NULL,
	action_id VARCHAR NOT NULL,
	provider_email_id VARCHAR(128),
	recipient_hash VARCHAR(64) NOT NULL,
	status VARCHAR(40) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_email_deliveries_action UNIQUE (action_id),
	CONSTRAINT uq_email_deliveries_case UNIQUE (case_id),
	CONSTRAINT uq_email_deliveries_provider_email UNIQUE (provider_email_id),
	FOREIGN KEY(session_id) REFERENCES demo_sessions (id),
	FOREIGN KEY(case_id) REFERENCES cases (id),
	FOREIGN KEY(action_id) REFERENCES actions (id)
)

;
CREATE INDEX ix_email_deliveries_recipient_created ON email_deliveries (recipient_hash, created_at);

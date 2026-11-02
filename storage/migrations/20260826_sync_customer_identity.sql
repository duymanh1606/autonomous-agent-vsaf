-- Backfill identities written to customer_profile before account_customers
-- started receiving the DOM-verified username/display name as well.
UPDATE account_customers AS customer
SET username = COALESCE(NULLIF(BTRIM(profile.username), ''), customer.username),
    metadata = CASE
        WHEN COALESCE(BTRIM(customer.metadata), '') IN ('', '{}')
             AND NULLIF(BTRIM(profile.display_name), '') IS NOT NULL
        THEN json_build_object('display_name', profile.display_name)::TEXT
        ELSE customer.metadata
    END,
    updated_at = CURRENT_TIMESTAMP
FROM customer_profile AS profile
JOIN conversations AS conversation
  ON conversation.account_id = profile.account_id
 AND conversation.platform_conversation_id = profile.conversation_id
WHERE customer.account_id = conversation.account_id
  AND customer.customer_id = conversation.customer_id
  AND (
      (customer.username = '' AND NULLIF(BTRIM(profile.username), '') IS NOT NULL)
      OR (
          COALESCE(BTRIM(customer.metadata), '') IN ('', '{}')
          AND NULLIF(BTRIM(profile.display_name), '') IS NOT NULL
      )
  );

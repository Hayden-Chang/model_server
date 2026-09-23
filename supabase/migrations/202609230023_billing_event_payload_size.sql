-- Apple Server Notifications V2 signedPayload can exceed the original 8 KiB
-- event replay limit (19,039 characters observed in sandbox). Keep a bounded
-- service-only replay field while allowing the signed certificate chain to fit.
alter table billing_private.billing_events
  drop constraint billing_events_replay_material_ciphertext_check;

alter table billing_private.billing_events
  add constraint billing_events_replay_material_ciphertext_check
  check (length(replay_material_ciphertext) between 1 and 131072);

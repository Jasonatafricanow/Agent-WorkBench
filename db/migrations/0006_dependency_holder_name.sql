-- 0006: who holds a wait, as plain text. A task can be with anyone (a courier, a shop, 路人甲) without that
-- person being a workspace member or reachable by a bot; responsible_principal_id stays for members.
ALTER TABLE dependencies ADD COLUMN holder_name TEXT;

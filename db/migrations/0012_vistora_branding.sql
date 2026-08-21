-- Rebrand only installation-owned defaults; preserve every user-customized name.
UPDATE workspaces
SET name = 'My Vistora', updated_at = now()
WHERE name = 'My FrameFactory';

UPDATE users
SET display_name = 'Vistora Owner', updated_at = now()
WHERE display_name = 'FrameFactory Owner';

UPDATE skills
SET publisher_name = 'Vistora', updated_at = now()
WHERE ownership_type = 'system'
  AND publisher_name IN ('FrameFactory', 'FrameFactory Official');

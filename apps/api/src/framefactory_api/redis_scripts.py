"""Atomic Redis queue transitions.

All keys use one Redis Cluster hash tag. Lua uses Redis server time so leases do
not depend on clock synchronization between API and worker processes.
"""

ENQUEUE = r"""
local previous_id = redis.call('HGET', KEYS[3], 'job_id')
if previous_id then
  if redis.call('HGET', KEYS[3], 'fingerprint') ~= ARGV[2] then
    return {-1, previous_id}
  end
  return {0, previous_id}
end
if redis.call('EXISTS', KEYS[1]) == 1 then return {-2, ARGV[1]} end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local available_at = now + tonumber(ARGV[7])
redis.call('HSET', KEYS[1],
  'id', ARGV[1], 'queue_name', ARGV[3], 'payload', ARGV[4],
  'status', 'queued', 'priority', ARGV[5], 'attempt_count', '0',
  'max_attempts', ARGV[6], 'available_at', available_at,
  'created_at', now, 'updated_at', now,
  'lease_owner', '', 'lease_token', '', 'lease_expires_at', '',
  'heartbeat_at', '', 'completed_at', '', 'result', '', 'error', '',
  'cancellation_requested', '0')
redis.call('ZADD', KEYS[2], available_at, ARGV[1])
redis.call('HSET', KEYS[3], 'job_id', ARGV[1], 'fingerprint', ARGV[2])
redis.call('EXPIRE', KEYS[3], ARGV[8])
return {1, ARGV[1]}
"""

CLAIM = r"""
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now, 'LIMIT', 0, ARGV[5])
for _, job_id in ipairs(expired) do
  local job_key = ARGV[1] .. ':job:' .. job_id
  local status = redis.call('HGET', job_key, 'status')
  local expires = tonumber(redis.call('HGET', job_key, 'lease_expires_at') or '0')
  if status == 'running' and expires <= now then
    local attempts = tonumber(redis.call('HGET', job_key, 'attempt_count') or '0')
    local maximum = tonumber(redis.call('HGET', job_key, 'max_attempts') or '0')
    local queue_name = redis.call('HGET', job_key, 'queue_name')
    if attempts >= maximum then
      redis.call('HSET', job_key, 'status', 'failed',
        'error', '{"code":"LEASE_EXPIRED","message":"Worker lease expired after final attempt"}',
        'completed_at', now, 'updated_at', now)
    else
      redis.call('HSET', job_key, 'status', 'retrying', 'available_at', now, 'updated_at', now)
      redis.call('ZADD', ARGV[1] .. ':ready:' .. queue_name, now, job_id)
    end
    redis.call('HSET', job_key, 'lease_owner', '', 'lease_token', '',
      'lease_expires_at', '', 'heartbeat_at', '')
  end
  redis.call('ZREM', KEYS[2], job_id)
end
local candidates = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'LIMIT', 0, ARGV[6])
local selected = nil
local selected_priority = -101
local selected_available = nil
local selected_created = nil
for _, job_id in ipairs(candidates) do
  local job_key = ARGV[1] .. ':job:' .. job_id
  local status = redis.call('HGET', job_key, 'status')
  if status ~= 'queued' and status ~= 'retrying' then
    redis.call('ZREM', KEYS[1], job_id)
  else
    local attempts = tonumber(redis.call('HGET', job_key, 'attempt_count') or '0')
    local maximum = tonumber(redis.call('HGET', job_key, 'max_attempts') or '0')
    if attempts >= maximum then
      redis.call('ZREM', KEYS[1], job_id)
      redis.call('HSET', job_key, 'status', 'failed',
        'error', '{"code":"ATTEMPTS_EXHAUSTED","message":"Job exhausted its attempts"}',
        'completed_at', now, 'updated_at', now)
    else
      local priority = tonumber(redis.call('HGET', job_key, 'priority') or '0')
      local available = tonumber(redis.call('HGET', job_key, 'available_at') or '0')
      local created = tonumber(redis.call('HGET', job_key, 'created_at') or '0')
      if not selected or priority > selected_priority or
        (priority == selected_priority and available < selected_available) or
        (priority == selected_priority and available == selected_available and
          created < selected_created) then
        selected, selected_priority = job_id, priority
        selected_available, selected_created = available, created
      end
    end
  end
end
if not selected then return {} end
local job_key = ARGV[1] .. ':job:' .. selected
local lease_expires = now + tonumber(ARGV[4])
redis.call('ZREM', KEYS[1], selected)
redis.call('HINCRBY', job_key, 'attempt_count', 1)
redis.call('HSET', job_key, 'status', 'running', 'lease_owner', ARGV[2],
  'lease_token', ARGV[3], 'lease_expires_at', lease_expires,
  'heartbeat_at', now, 'updated_at', now)
if not redis.call('HGET', job_key, 'started_at') then
  redis.call('HSET', job_key, 'started_at', now)
end
redis.call('ZADD', KEYS[2], lease_expires, selected)
return redis.call('HGETALL', job_key)
"""

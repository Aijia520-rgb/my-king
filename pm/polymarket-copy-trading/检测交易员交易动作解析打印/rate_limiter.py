import asyncio
import time
from config import logger

class RateLimiter:
    """
    令牌桶限流器 (Token Bucket)
    用于全局控制 Data API 的请求速率
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(RateLimiter, cls).__new__(cls)
        return cls._instance

    def __init__(self, rate=18, capacity=180):
        """
        初始化限流器
        
        Args:
            rate: 填充速率 (令牌/秒)。默认 18 (即 180 req/10s，略低于官方 200 req/10s)
            capacity: 桶容量。默认 180
        """
        if not hasattr(self, 'initialized'):
            self.rate = rate
            self.capacity = capacity
            self.tokens = capacity
            self.last_refill = time.time()
            self.lock = None
            self.initialized = True
            logger.info(f"[RATE-LIMIT] 限流器已初始化: {rate} req/s, 容量 {capacity}")

    async def acquire(self, tokens=1):
        """
        获取令牌。如果桶空了，则等待直到有足够的令牌。
        """
        if self.lock is None:
            self.lock = asyncio.Lock()

        async with self.lock:
            now = time.time()
            # 1. 补充令牌
            elapsed = now - self.last_refill
            new_tokens = elapsed * self.rate
            if new_tokens > 0:
                self.tokens = min(self.capacity, self.tokens + new_tokens)
                self.last_refill = now
            
            # 2. 检查令牌是否足够
            if self.tokens >= tokens:
                self.tokens -= tokens
                return
            
            # 3. 令牌不足，计算需要等待的时间
            deficit = tokens - self.tokens
            wait_time = deficit / self.rate
            
            # logger.debug(f"[RATE-LIMIT] 触发限流，等待 {wait_time:.2f}s (当前令牌: {self.tokens:.2f})")
            
            # 4. 预支令牌（实际上是让当前请求等待，但为了简化逻辑，我们让它等待后直接通过）
            # 这里我们选择 sleep，释放锁，让其他协程也有机会（虽然在锁内 sleep 会阻塞所有请求，这正是我们想要的——全局暂停）
            # 但为了避免锁竞争过久，我们可以在这里不释放锁直接 sleep？
            # 不，asyncio.Lock 在 await sleep 时不会释放。
            # 这意味着所有请求都会排队。这对于严格限流是正确的。
            
            await asyncio.sleep(wait_time)
            
            # 等待结束后，扣除令牌（此时 tokens 应该已经通过时间流逝补充上来了）
            # 重新计算 refill 以保持精确
            now_after_wait = time.time()
            elapsed_after_wait = now_after_wait - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed_after_wait * self.rate)
            self.last_refill = now_after_wait
            
            self.tokens -= tokens

# 全局单例
global_rate_limiter = RateLimiter()
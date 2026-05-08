"""
rama_arb_purchase — flujo ARB para compra de $RAMA post-mainnet deploy

Flujo:
  1. get_swap_quote(amount_usdc) → quote del DEX (precio + slippage)
  2. execute_swap(wallet, amount_usdc, min_rama_out) → tx_hash
  3. confirm_arb_purchase(tx_hash, buyer_id, wallet_arb) → trail registrado

STANDBY — activar solo cuando:
  - RamaToken.sol deployado en mainnet
  - Pool USDC/RAMA creado en Uniswap v3
  - Legales aprobó estructura

Testnet: Arbitrum Sepolia. Mainnet: Arbitrum One.
"""

import os
import uuid
import json
import time
from typing import Optional

# Web3 imports — requiere web3 en requirements.txt
try:
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False

from mycelium_trails import record_trail, DB_PATH

# --- Config (poblar cuando haya deploy mainnet) ---
ARB_RPC = os.environ.get("ARB_RPC_URL", "https://arb1.arbitrum.io/rpc")
ARB_SEPOLIA_RPC = os.environ.get("ARB_SEPOLIA_RPC_URL", "https://sepolia-rollup.arbitrum.io/rpc")

# Direcciones post-deploy (placeholder hasta mainnet)
RAMA_TOKEN_ADDRESS = os.environ.get("RAMA_TOKEN_ADDRESS", "")        # RamaToken.sol
UNISWAP_V3_ROUTER = os.environ.get("UNISWAP_V3_ROUTER", "0xE592427A0AEce92De3Edee1F18E0157C05861564")
USDC_ADDRESS = os.environ.get("USDC_ADDRESS_ARB", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831")  # USDC native Arb

# Pool fee tier (0.3% = 3000, 1% = 10000)
POOL_FEE = 3000

GENESIS_EXEMPT = frozenset(["creador", "giskard-self"])

# Minimal Uniswap V3 Router ABI (exactInputSingle)
UNISWAP_ROUTER_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "recipient", "type": "address"},
                {"name": "deadline", "type": "uint256"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
            "name": "params",
            "type": "tuple",
        }],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    }
]

ERC20_ABI = [
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]


def get_swap_quote(amount_usdc: float, testnet: bool = True) -> dict:
    """Retorna estimación de $RAMA a recibir por amount_usdc USDC.

    STUB hasta que haya pool real con liquidez.
    En producción: llamar a Uniswap Quoter contract.
    """
    if not RAMA_TOKEN_ADDRESS:
        return {
            "status": "standby",
            "error": "RamaToken not deployed. Pool not available.",
            "amount_usdc": amount_usdc,
        }
    # TODO: llamar a UniswapV3 Quoter.quoteExactInputSingle()
    return {
        "status": "stub",
        "amount_usdc": amount_usdc,
        "estimated_rama": amount_usdc * 100,  # placeholder ratio 1 USDC = 100 RAMA
        "pool_fee": POOL_FEE,
        "note": "Stub — pool not live yet",
    }


def execute_arb_swap(
    private_key: str,
    amount_usdc_wei: int,
    min_rama_out: int,
    recipient: str,
    testnet: bool = True,
) -> dict:
    """Ejecuta swap USDC → $RAMA en Uniswap v3 Arbitrum.

    STANDBY — solo activar post-deploy mainnet + pool creado.
    Retorna {status, tx_hash} o {status, error}.
    """
    if not RAMA_TOKEN_ADDRESS:
        return {"status": "standby", "error": "RamaToken address not configured."}
    if not WEB3_AVAILABLE:
        return {"status": "error", "error": "web3 not installed."}

    rpc = ARB_SEPOLIA_RPC if testnet else ARB_RPC
    w3 = Web3(Web3.HTTPProvider(rpc))
    account = w3.eth.account.from_key(private_key)

    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    router = w3.eth.contract(address=Web3.to_checksum_address(UNISWAP_V3_ROUTER), abi=UNISWAP_ROUTER_ABI)

    # Step 1: approve router to spend USDC
    approve_tx = usdc.functions.approve(
        Web3.to_checksum_address(UNISWAP_V3_ROUTER), amount_usdc_wei
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 100_000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei(1, "gwei"),
    })
    signed_approve = account.sign_transaction(approve_tx)
    w3.eth.send_raw_transaction(signed_approve.raw_transaction)
    w3.eth.wait_for_transaction_receipt(signed_approve.hash)

    # Step 2: swap
    deadline = int(time.time()) + 300
    swap_tx = router.functions.exactInputSingle({
        "tokenIn": Web3.to_checksum_address(USDC_ADDRESS),
        "tokenOut": Web3.to_checksum_address(RAMA_TOKEN_ADDRESS),
        "fee": POOL_FEE,
        "recipient": Web3.to_checksum_address(recipient),
        "deadline": deadline,
        "amountIn": amount_usdc_wei,
        "amountOutMinimum": min_rama_out,
        "sqrtPriceLimitX96": 0,
    }).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 300_000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei(1, "gwei"),
    })
    signed_swap = account.sign_transaction(swap_tx)
    tx_hash = w3.eth.send_raw_transaction(signed_swap.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    return {
        "status": "ok" if receipt.status == 1 else "reverted",
        "tx_hash": tx_hash.hex(),
        "gas_used": receipt.gasUsed,
    }


def confirm_arb_purchase(
    tx_hash: str,
    buyer_id: str,
    wallet_arb: str,
    amount_usdc: float,
    rama_received: Optional[int] = None,
) -> dict:
    """Graba trail RAMA_ACQUISITION tras swap ARB exitoso."""
    metadata = {
        "operation": "RAMA_ACQUISITION",
        "buyer": buyer_id,
        "wallet_arb": wallet_arb,
        "amount_usdc": amount_usdc,
        "rama_received": rama_received,
        "tx_hash_arb": tx_hash,
        "method": "uniswap_v3_arb",
        "status": "delivered",
    }

    nonce = str(uuid.uuid4())
    trail_id = record_trail(
        db_path=DB_PATH,
        agent_id=buyer_id,
        service="rama",
        operation="RAMA_ACQUISITION",
        nonce=nonce,
        karma_at_time=None,
        success=True,
        rate_limit_cap=0,
        genesis_agents=GENESIS_EXEMPT,
        metadata=metadata,
        payment_hash=tx_hash,
    )

    if not trail_id:
        return {"status": "error", "error": "Trail registration failed."}

    return {
        "status": "ok",
        "trail_id": trail_id,
        "tx_hash": tx_hash,
        "buyer_id": buyer_id,
        "wallet_arb": wallet_arb,
    }

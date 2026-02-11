// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

enum TaskStatus {
    NONE,       // 0 - default
    CREATED,    // 1
    FUNDED,     // 2
    CLAIMED,    // 3
    SUBMITTED,  // 4
    ACCEPTED,   // 5
    SETTLED,    // 6
    REJECTED,   // 7
    EXPIRED,    // 8
    CANCELLED,  // 9
    REFUNDED    // 10
}

struct Task {
    address boss;
    uint64 expiry;
    TaskStatus status;
    uint8 maxRetries;
    uint8 retryCount;
    // Slot 2
    address worker;
    uint96 amount;
    // Slot 3
    bytes32 contentHash;
}

interface ITaskEscrow {
    event TaskCreated(bytes32 indexed taskId, address indexed boss, uint96 amount);
    event TaskFunded(bytes32 indexed taskId, uint96 amount);
    event TaskClaimed(bytes32 indexed taskId, address indexed worker);
    event TaskSubmitted(bytes32 indexed taskId, bytes32 resultHash);
    event VerdictReceived(bytes32 indexed taskId, bool accepted, uint8 score);
    event TaskSettled(bytes32 indexed taskId, address indexed worker, uint256 payout, uint256 fee);
    event TaskRejected(bytes32 indexed taskId);
    event TaskPaused(bytes32 indexed taskId);
    event TaskExpired(bytes32 indexed taskId);
    event TaskCancelled(bytes32 indexed taskId);
    event TaskRefunded(bytes32 indexed taskId, uint256 amount);

    function createTask(uint96 amount, uint64 expiry, bytes32 contentHash, uint8 maxRetries) external returns (bytes32 taskId);
    function fundTask(bytes32 taskId) external;
    function cancelTask(bytes32 taskId) external;
    function claimTask(bytes32 taskId) external;
    function submitResult(bytes32 taskId, bytes32 resultHash) external;
    function onVerdictReceived(bytes32 taskId, bool accepted, uint8 score) external;
    function settle(bytes32 taskId) external;
    function markExpired(bytes32 taskId) external;
    function refund(bytes32 taskId) external;
    function withdraw() external;
    
    function getTask(bytes32 taskId) external view returns (Task memory);
}

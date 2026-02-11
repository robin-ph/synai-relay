// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import "./interfaces/ITaskEscrow.sol";

contract CVSOracle is Ownable {
    struct Verdict {
        bytes32 taskId;
        bool accepted;
        uint8 score;
        bytes32 evidenceHash;
        uint64 timestamp;
    }

    event VerdictSubmitted(bytes32 indexed taskId, address indexed oracle, bool accepted, uint8 score, bytes32 evidenceHash);

    address public oracle;
    address public taskEscrow;
    mapping(bytes32 => Verdict) public verdicts;

    modifier onlyOracle() {
        require(msg.sender == oracle, "Only oracle");
        _;
    }

    constructor(address _oracle, address _taskEscrow) Ownable(msg.sender) {
        oracle = _oracle;
        taskEscrow = _taskEscrow;
    }

    function setOracle(address _oracle) external onlyOwner {
        oracle = _oracle;
    }

    function setTaskEscrow(address _taskEscrow) external onlyOwner {
        taskEscrow = _taskEscrow;
    }

    function submitVerdict(bytes32 taskId, bool accepted, uint8 score, bytes32 evidenceHash) external onlyOracle {
        require(verdicts[taskId].timestamp == 0, "Already verdict");
        
        verdicts[taskId] = Verdict({
            taskId: taskId,
            accepted: accepted,
            score: score,
            evidenceHash: evidenceHash,
            timestamp: uint64(block.timestamp)
        });

        ITaskEscrow(taskEscrow).onVerdictReceived(taskId, accepted, score);
        
        emit VerdictSubmitted(taskId, msg.sender, accepted, score, evidenceHash);
    }

    function getVerdict(bytes32 taskId) external view returns (Verdict memory) {
        return verdicts[taskId];
    }
}

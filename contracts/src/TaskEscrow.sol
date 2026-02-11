import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import "./interfaces/ITaskEscrow.sol";

contract TaskEscrow is ITaskEscrow, ReentrancyGuard, Ownable {
    using SafeERC20 for IERC20;

    IERC20 public immutable usdc;
    address public treasury;
    address public oracle;
    uint16 public defaultFeeBps = 500; // 5%

    mapping(bytes32 => Task) public tasks;
    mapping(address => uint256) public pendingWithdrawals;
    mapping(address => uint256) private _nonces;

    modifier onlyOracle() {
        require(msg.sender == oracle, "Only oracle");
        _;
    }

    constructor(address _usdc, address _treasury, uint16 _feeBps) Ownable(msg.sender) {
        usdc = IERC20(_usdc);
        treasury = _treasury;
        defaultFeeBps = _feeBps;
    }

    function setOracle(address _oracle) external onlyOwner {
        oracle = _oracle;
    }

    function setTreasury(address _treasury) external onlyOwner {
        treasury = _treasury;
    }

    function setDefaultFeeBps(uint16 _feeBps) external onlyOwner {
        defaultFeeBps = _feeBps;
    }

    function createTask(uint96 amount, uint64 expiry, bytes32 contentHash, uint8 maxRetries) external nonReentrant returns (bytes32 taskId) {
        require(expiry > block.timestamp, "Expiry in past");
        require(amount > 0, "Amount zero");

        taskId = keccak256(abi.encodePacked(msg.sender, block.chainid, _nonces[msg.sender]++));
        
        tasks[taskId] = Task({
            boss: msg.sender,
            expiry: expiry,
            status: TaskStatus.CREATED,
            maxRetries: maxRetries,
            retryCount: 0,
            worker: address(0),
            amount: amount,
            contentHash: contentHash
        });

        emit TaskCreated(taskId, msg.sender, amount);
    }

    function fundTask(bytes32 taskId) external nonReentrant {
        Task storage task = tasks[taskId];
        require(task.boss == msg.sender, "Only boss");
        require(task.status == TaskStatus.CREATED, "Not created");

        usdc.safeTransferFrom(msg.sender, address(this), uint256(task.amount));
        task.status = TaskStatus.FUNDED;
        
        emit TaskFunded(taskId, task.amount);
    }

    function claimTask(bytes32 taskId) external nonReentrant {
        Task storage task = tasks[taskId];
        require(task.status == TaskStatus.FUNDED, "Not funded");
        require(block.timestamp <= task.expiry, "Expired");
        
        task.worker = msg.sender;
        task.status = TaskStatus.CLAIMED;
        
        emit TaskClaimed(taskId, msg.sender);
    }

    function submitResult(bytes32 taskId, bytes32 resultHash) external nonReentrant {
        Task storage task = tasks[taskId];
        require(task.status == TaskStatus.CLAIMED || task.status == TaskStatus.REJECTED, "Not claim/rej");
        require(task.worker == msg.sender, "Only worker");
        require(block.timestamp <= task.expiry, "Expired");

        task.status = TaskStatus.SUBMITTED;
        emit TaskSubmitted(taskId, resultHash);
    }

    function onVerdictReceived(bytes32 taskId, bool accepted, uint8 score) external override onlyOracle {
        Task storage task = tasks[taskId];
        require(task.status == TaskStatus.SUBMITTED, "Not submitted");

        if (accepted) {
            task.status = TaskStatus.ACCEPTED;
        } else {
            task.status = TaskStatus.REJECTED;
            task.retryCount++;
            emit TaskRejected(taskId);

            if (task.retryCount >= task.maxRetries) {
                task.status = TaskStatus.EXPIRED; // Treat as expired for refund logic, or implement PAUSED
                emit TaskPaused(taskId);
            }
        }
        
        emit VerdictReceived(taskId, accepted, score);
    }

    function settle(bytes32 taskId) external nonReentrant {
        Task storage task = tasks[taskId];
        require(task.status == TaskStatus.ACCEPTED, "Not accepted");

        uint256 fee = (uint256(task.amount) * defaultFeeBps) / 10000;
        uint256 payout = uint256(task.amount) - fee;

        task.status = TaskStatus.SETTLED;
        
        pendingWithdrawals[task.worker] += payout;
        pendingWithdrawals[treasury] += fee;

        emit TaskSettled(taskId, task.worker, payout, fee);
    }

    function markExpired(bytes32 taskId) external nonReentrant {
        Task storage task = tasks[taskId];
        require(block.timestamp > task.expiry, "Not expired");
        require(
            task.status == TaskStatus.FUNDED || 
            task.status == TaskStatus.CLAIMED || 
            task.status == TaskStatus.SUBMITTED || 
            task.status == TaskStatus.REJECTED,
            "Cannot expire"
        );

        task.status = TaskStatus.EXPIRED;
        emit TaskExpired(taskId);
    }

    function refund(bytes32 taskId) external nonReentrant {
        Task storage task = tasks[taskId];
        require(msg.sender == task.boss, "Only boss");
        require(task.status == TaskStatus.EXPIRED || task.status == TaskStatus.CANCELLED || task.status == TaskStatus.REFUNDED, "Not refundable state");
        
        // If already refunded do nothing? But we check status.
        // If expired or cancelled, move funds to pending.
        // Actually if CANCELLED it should happen in cancelTask? 
        // Spec says: cancelTask -> CANCELLED. refund -> REFUNDED.
        
        // Let's follow strict state machine.
        // If EXPIRED or CANCELLED, we can refund.
        // But cancelTask requires worker==0.
        
        require(task.status != TaskStatus.REFUNDED, "Already refunded");

        uint256 amount = uint256(task.amount);
        task.status = TaskStatus.REFUNDED;
        
        pendingWithdrawals[task.boss] += amount;
        
        emit TaskRefunded(taskId, amount);
    }

    function cancelTask(bytes32 taskId) external nonReentrant {
        Task storage task = tasks[taskId];
        require(msg.sender == task.boss, "Only boss");
        require(task.status == TaskStatus.CREATED || task.status == TaskStatus.FUNDED, "Cannot cancel");
        require(task.worker == address(0), "Worker exists");

        if (task.status == TaskStatus.CREATED) {
            // No funds were deposited, so set amount to 0 to prevent refund() from minting claims
            task.amount = 0;
        }
        
        task.status = TaskStatus.CANCELLED;
        emit TaskCancelled(taskId);
    }

    function withdraw() external nonReentrant {
        uint256 amount = pendingWithdrawals[msg.sender];
        require(amount > 0, "No funds");
        
        pendingWithdrawals[msg.sender] = 0;
        usdc.safeTransfer(msg.sender, amount);
    }

    function getTask(bytes32 taskId) external view returns (Task memory) {
        return tasks[taskId];
    }
}

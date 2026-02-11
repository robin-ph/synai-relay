// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "forge-std/Test.sol";
import "../src/TaskEscrow.sol";
import "../test/mocks/MockUSDC.sol";

contract TaskEscrowTest is Test {
    TaskEscrow public escrow;
    MockUSDC public usdc;
    address public boss = address(0x1);
    address public worker = address(0x2);
    address public treasury = address(0x3);
    address public oracle = address(0x4);

    event TaskCreated(bytes32 indexed taskId, address indexed boss, uint96 amount);
    event TaskFunded(bytes32 indexed taskId, uint96 amount);
    event TaskClaimed(bytes32 indexed taskId, address indexed worker);
    event TaskSubmitted(bytes32 indexed taskId, bytes32 resultHash);
    event TaskSettled(bytes32 indexed taskId, address indexed worker, uint256 payout, uint256 fee);
    event TaskCancelled(bytes32 indexed taskId);
    event TaskRefunded(bytes32 indexed taskId, uint256 amount);

    function setUp() public {
        usdc = new MockUSDC();
        escrow = new TaskEscrow(address(usdc), treasury, 500); // 5% fee
        escrow.setOracle(oracle);
        
        vm.label(boss, "Boss");
        vm.label(worker, "Worker");
        vm.label(treasury, "Treasury");
        vm.label(oracle, "Oracle");
        
        usdc.mint(boss, 10000 * 10**6); // 10k USDC
        vm.prank(boss);
        usdc.approve(address(escrow), type(uint256).max);
    }

    function _createTask() internal returns (bytes32) {
        vm.prank(boss);
        return escrow.createTask(100 * 10**6, uint64(block.timestamp + 1 days), bytes32("content"), 3);
    }

    function test_createTask_generates_unique_taskId() public {
        bytes32 id1 = _createTask();
        bytes32 id2 = _createTask();
        assertTrue(id1 != id2);
    }

    function test_createTask_stores_correct_data() public {
        bytes32 id = _createTask();
        Task memory task = escrow.getTask(id);
        
        assertEq(task.boss, boss);
        assertEq(task.amount, 100 * 10**6);
        assertEq(uint(task.status), uint(TaskStatus.CREATED));
    }

    function test_fundTask_transfers_usdc_to_contract() public {
        bytes32 id = _createTask();
        
        vm.expectEmit(true, false, false, true);
        emit TaskFunded(id, 100 * 10**6);
        
        vm.prank(boss);
        escrow.fundTask(id);
        
        assertEq(usdc.balanceOf(address(escrow)), 100 * 10**6);
        Task memory task = escrow.getTask(id);
        assertEq(uint(task.status), uint(TaskStatus.FUNDED));
    }

    function test_fundTask_reverts_if_not_boss() public {
        bytes32 id = _createTask();
        vm.prank(worker);
        vm.expectRevert("Only boss");
        escrow.fundTask(id);
    }

    function test_fundTask_reverts_if_not_CREATED() public {
        bytes32 id = _createTask();
        vm.prank(boss);
        escrow.fundTask(id);
        
        vm.prank(boss);
        vm.expectRevert("Not created");
        escrow.fundTask(id);
    }

    function test_claimTask_sets_worker() public {
        bytes32 id = _createTask();
        vm.prank(boss);
        escrow.fundTask(id);
        
        vm.prank(worker);
        escrow.claimTask(id);
        
        Task memory task = escrow.getTask(id);
        assertEq(task.worker, worker);
        assertEq(uint(task.status), uint(TaskStatus.CLAIMED));
    }

    function test_claimTask_reverts_if_not_FUNDED() public {
        bytes32 id = _createTask();
        vm.prank(worker);
        vm.expectRevert("Not funded");
        escrow.claimTask(id);
    }

    function test_claimTask_reverts_if_expired() public {
        bytes32 id = _createTask();
        vm.prank(boss);
        escrow.fundTask(id);
        
        vm.warp(block.timestamp + 2 days);
        vm.prank(worker);
        vm.expectRevert("Expired");
        escrow.claimTask(id);
    }

    function test_submitResult_requires_worker() public {
        bytes32 id = _createTask();
        vm.prank(boss);
        escrow.fundTask(id);
        vm.prank(worker);
        escrow.claimTask(id);
        
        vm.prank(boss);
        vm.expectRevert("Only worker");
        escrow.submitResult(id, bytes32("res"));
    }

    function test_cancelTask_refunds_if_funded_via_pull() public {
        bytes32 id = _createTask();
        vm.prank(boss);
        escrow.fundTask(id);
        
        vm.prank(boss);
        escrow.cancelTask(id);
        
        Task memory task = escrow.getTask(id);
        assertEq(uint(task.status), uint(TaskStatus.CANCELLED));
        
        // Refund step
        vm.prank(boss);
        escrow.refund(id);
        
        vm.prank(boss);
        escrow.withdraw();
        
        assertEq(usdc.balanceOf(boss), 10000 * 10**6);
    }
    
    function test_cancelTask_reverts_if_worker_exists() public {
        bytes32 id = _createTask();
        vm.prank(boss);
        escrow.fundTask(id);
        vm.prank(worker);
        escrow.claimTask(id);
        
        vm.prank(boss);
        vm.expectRevert("Cannot cancel");
        escrow.cancelTask(id);
    }

    function test_markExpired_works_after_deadline() public {
        bytes32 id = _createTask();
        vm.prank(boss);
        escrow.fundTask(id);
        
        vm.warp(block.timestamp + 2 days);
        escrow.markExpired(id);
        
        Task memory task = escrow.getTask(id);
        assertEq(uint(task.status), uint(TaskStatus.EXPIRED));
    }

    function test_refund_after_EXPIRED() public {
        bytes32 id = _createTask();
        vm.prank(boss);
        escrow.fundTask(id);
        
        vm.warp(block.timestamp + 2 days);
        escrow.markExpired(id);
        
        vm.prank(boss);
        escrow.refund(id);
        
        assertEq(escrow.pendingWithdrawals(boss), 100 * 10**6);
    }

    // Settlement Logic
    function test_settle_happy_path_95_5_split() public {
        bytes32 id = _createTask();
        vm.prank(boss);
        escrow.fundTask(id);
        vm.prank(worker);
        escrow.claimTask(id);
        vm.prank(worker);
        escrow.submitResult(id, bytes32("res"));
        
        vm.prank(oracle);
        escrow.onVerdictReceived(id, true, 100);
        
        escrow.settle(id);
        
        uint256 fee = 100 * 10**6 * 500 / 10000; // 5 * 10**6
        uint256 payout = 100 * 10**6 - fee; // 95 * 10**6
        
        assertEq(escrow.pendingWithdrawals(worker), payout);
        assertEq(escrow.pendingWithdrawals(treasury), fee);
    }

    function test_withdraw_transfers_correct_amount() public {
        // Setup pending withdrawals
        test_settle_happy_path_95_5_split();
        
        uint256 balBefore = usdc.balanceOf(worker);
        vm.prank(worker);
        escrow.withdraw();
        assertEq(usdc.balanceOf(worker) - balBefore, 95 * 10**6);
        assertEq(escrow.pendingWithdrawals(worker), 0);
    }
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "forge-std/Test.sol";
import "../src/CVSOracle.sol";
import "../src/TaskEscrow.sol";
import "../test/mocks/MockUSDC.sol";

contract CVSOracleTest is Test {
    CVSOracle public oracleContract;
    TaskEscrow public escrow;
    MockUSDC public usdc;
    
    address public admin = address(this);
    address public oracleSigner = address(0x99);
    address public boss = address(0x1);
    address public worker = address(0x2);
    
    function setUp() public {
        usdc = new MockUSDC();
        escrow = new TaskEscrow(address(usdc), address(0x3), 500);
        oracleContract = new CVSOracle(oracleSigner, address(escrow));
        escrow.setOracle(address(oracleContract));
        
        vm.label(boss, "Boss");
        vm.label(worker, "Worker");
        vm.label(oracleSigner, "OracleSigner");
        
        usdc.mint(boss, 1000 * 10**6);
        vm.prank(boss);
        usdc.approve(address(escrow), type(uint256).max);
    }

    function test_submitVerdict_accept_triggers_callback() public {
        vm.prank(boss);
        bytes32 taskId = escrow.createTask(100 * 10**6, uint64(block.timestamp + 1000), bytes32("C"), 3);
        vm.prank(boss);
        escrow.fundTask(taskId);
        vm.prank(worker);
        escrow.claimTask(taskId);
        vm.prank(worker);
        escrow.submitResult(taskId, bytes32("R"));
        
        vm.prank(oracleSigner);
        oracleContract.submitVerdict(taskId, true, 95, bytes32("E"));
        
        Task memory task = escrow.getTask(taskId);
        assertEq(uint(task.status), uint(TaskStatus.ACCEPTED));
    }
    
    function test_submitVerdict_reverts_if_not_oracle() public {
        vm.expectRevert("Only oracle");
        oracleContract.submitVerdict(bytes32(0), true, 0, bytes32(0));
    }

    function test_submitVerdict_reject_retry_count() public {
        vm.prank(boss);
        bytes32 taskId = escrow.createTask(100 * 10**6, uint64(block.timestamp + 1000), bytes32("C"), 3);
        vm.prank(boss);
        escrow.fundTask(taskId);
        vm.prank(worker);
        escrow.claimTask(taskId);
        vm.prank(worker);
        escrow.submitResult(taskId, bytes32("R"));
        
        vm.prank(oracleSigner);
        oracleContract.submitVerdict(taskId, false, 20, bytes32("E"));
        
        Task memory task = escrow.getTask(taskId);
        assertEq(uint(task.status), uint(TaskStatus.REJECTED));
        assertEq(task.retryCount, 1);
    }
}

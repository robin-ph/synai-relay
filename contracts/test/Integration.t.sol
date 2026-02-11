// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "forge-std/Test.sol";
import "../src/CVSOracle.sol";
import "../src/TaskEscrow.sol";
import "../test/mocks/MockUSDC.sol";

contract IntegrationTest is Test {
    CVSOracle public oracleContract;
    TaskEscrow public escrow;
    MockUSDC public usdc;
    
    address public admin = address(this);
    address public oracleSigner = address(0x99);
    address public boss = address(0x1);
    address public worker = address(0x2);
    address public treasury = address(0x3);
    
    function setUp() public {
        usdc = new MockUSDC();
        escrow = new TaskEscrow(address(usdc), treasury, 500);
        oracleContract = new CVSOracle(oracleSigner, address(escrow));
        escrow.setOracle(address(oracleContract));
        
        vm.label(boss, "Boss");
        vm.label(worker, "Worker");
        vm.label(treasury, "Treasury");
        vm.label(oracleSigner, "OracleSigner");
        
        usdc.mint(boss, 100000 ether);
        vm.prank(boss);
        usdc.approve(address(escrow), type(uint256).max);
    }

    function test_full_lifecycle_happy_path() public {
        // Create
        vm.prank(boss);
        bytes32 taskId = escrow.createTask(100 ether, uint64(block.timestamp + 1000), bytes32("C"), 3);
        
        // Fund
        vm.prank(boss);
        escrow.fundTask(taskId);
        
        // Claim
        vm.prank(worker);
        escrow.claimTask(taskId);
        
        // Submit
        vm.prank(worker);
        escrow.submitResult(taskId, bytes32("R"));
        
        // Verdict
        vm.prank(oracleSigner);
        oracleContract.submitVerdict(taskId, true, 95, bytes32("E"));
        
        // Settle
        escrow.settle(taskId);
        
        // Withdraw
        vm.prank(worker);
        escrow.withdraw();
        
        assertEq(usdc.balanceOf(worker), 95 ether);
        assertEq(escrow.pendingWithdrawals(treasury), 5 ether);
    }

    function test_circuit_breaker_pauses_at_max_retries() public {
        vm.prank(boss);
        bytes32 taskId = escrow.createTask(100 ether, uint64(block.timestamp + 1000), bytes32("C"), 3);
        vm.prank(boss);
        escrow.fundTask(taskId);
        vm.prank(worker);
        escrow.claimTask(taskId);
        
        // Mock oracle to allow multiple verdicts (since actual CVSOracle is strict)
        escrow.setOracle(address(this));
        
        for(int i=0; i<3; i++) {
            vm.prank(worker);
            escrow.submitResult(taskId, bytes32("R"));
            
            // Rejects
            escrow.onVerdictReceived(taskId, false, 0);
        }
        
        Task memory t = escrow.getTask(taskId);
        assertEq(uint(t.status), uint(TaskStatus.EXPIRED));
    }
}

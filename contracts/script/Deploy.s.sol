// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "forge-std/Script.sol";
import "../src/TaskEscrow.sol";
import "../src/CVSOracle.sol";

contract DeployScript is Script {
    function run() external {
        uint256 deployerPrivateKey = vm.envUint("DEPLOYER_PRIVATE_KEY");
        address usdcAddress = vm.envAddress("USDC_ADDRESS");
        address treasuryAddress = vm.envAddress("TREASURY_ADDRESS");
        address oracleEOA = vm.envAddress("ORACLE_EOA");

        vm.startBroadcast(deployerPrivateKey);

        // Deploy TaskEscrow
        TaskEscrow escrow = new TaskEscrow(usdcAddress, treasuryAddress, 500);

        // Deploy CVSOracle
        CVSOracle oracle = new CVSOracle(oracleEOA, address(escrow));

        // Set Oracle in Escrow
        escrow.setOracle(address(oracle));

        vm.stopBroadcast();
        
        console.log("TaskEscrow deployed at:", address(escrow));
        console.log("CVSOracle deployed at:", address(oracle));
    }
}

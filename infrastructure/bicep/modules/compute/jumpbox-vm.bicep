// jumpbox-vm.bicep
//
// Purpose: The debug/access jump box VM (rsbc-ai-jumpbox) -- exists solely
// to reach resources with publicNetworkAccess=Disabled (Service Bus,
// Document Intelligence) from a desktop session, since neither the Portal
// nor a laptop with no VNet route can reach them directly. See
// docs/deployment/jumpbox-setup.md for the full rationale and how to use
// what this deploys.
//
// Not created here, platform-policy-owned: AzureMonitorLinuxAgent,
// AzurePolicyforLinux, ChangeTracking-Linux -- this landing zone's own
// Azure Policy Deploy-if-not-exists rules attach these automatically to
// any VM; declaring them here would just fight that policy, not complement
// it (same reasoning as main.bicep's header on Private DNS).
//
// The VM's own subnet is referenced, not created -- it's an existing
// subnet inside the platform VNet (see subscription.bicep's pattern for
// why subnet creation is a separate, VNet-resource-group-scoped concern).

@description('VM name, e.g. rsbc-ai-jumpbox.')
param name string

@description('Azure region.')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('VM size. Standard_DC2s_v3 (Confidential Computing, 2 vCPU) matches the VM this module was written to capture -- see docs/deployment/jumpbox-setup.md for the performance implications discovered while using it (no GPU on this SKU family; the actual RDP lag root-caused to xrdp\'s own encoding, not this).')
param vmSize string = 'Standard_DC2s_v3'

@description('Resource ID of the existing subnet this VM\'s NIC attaches to.')
@minLength(1)
param subnetId string

@description('Local admin username -- also the RDP login user jumpbox-setup.sh configures the desktop session for.')
@minLength(1)
param adminUsername string

@secure()
@description('Local admin password. Required for RDP login (which needs password auth, unlike the AAD SSH extension below) -- supply via a pipeline secret, never a parameters.json file.')
param adminPassword string

@description('Raw URL to fetch scripts/debug/sb_queue_viewer.py from -- see jumpbox-setup.sh\'s header for why this isn\'t pinned to a release/commit for this particular (internal, low-stakes) tool.')
param queueViewerRawUrl string = 'https://raw.githubusercontent.com/bcgov/rsbc-ai-dmer-automation/main/scripts/debug/sb_queue_viewer.py'

@description('Raw URL to fetch this same repository\'s scripts/deployment/jumpbox-setup.sh from -- what the CustomScript extension actually downloads and runs.')
param setupScriptRawUrl string = 'https://raw.githubusercontent.com/bcgov/rsbc-ai-dmer-automation/main/scripts/deployment/jumpbox-setup.sh'

resource nic 'Microsoft.Network/networkInterfaces@2023-11-01' = {
  name: '${name}-nic'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: {
            id: subnetId
          }
          privateIPAllocationMethod: 'Dynamic'
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2023-09-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    // System-assigned -- granted Service Bus data-plane read access (see
    // jumpbox.bicep's role assignment) so sb_queue_viewer.py's
    // ManagedIdentityCredential auth works with zero interactive login.
    type: 'SystemAssigned'
  }
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    osProfile: {
      computerName: name
      adminUsername: adminUsername
      adminPassword: adminPassword
      linuxConfiguration: {
        // Password auth stays enabled -- RDP needs it. SSH access is
        // expected to go through the AADSSHLoginForLinux extension below
        // instead, not a traditional SSH keypair.
        disablePasswordAuthentication: false
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        managedDisk: {
          storageAccountType: 'Premium_LRS'
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
  }
}

// AAD-based SSH login -- what `az network bastion ssh --auth-type AAD`
// authenticates against. A separate identity/home-directory context from
// adminUsername's own login -- see jumpbox-setup.sh's header and
// docs/deployment/jumpbox-setup.md for the home-directory mismatch bug
// this caused when files were created by hand through an AAD SSH session.
resource aadSshLogin 'Microsoft.Compute/virtualMachines/extensions@2023-09-01' = {
  parent: vm
  name: 'AADSSHLoginForLinux'
  location: location
  properties: {
    publisher: 'Microsoft.Azure.ActiveDirectory'
    type: 'AADSSHLoginForLinux'
    typeHandlerVersion: '1.0'
    autoUpgradeMinorVersion: true
  }
}

// Provisions the desktop session (XFCE + GNOME, switchable) and
// sb_queue_viewer.py -- see jumpbox-setup.sh for exactly what this runs.
// Re-running this deployment with a different queueViewerRawUrl/
// setupScriptRawUrl (or any other property change) is what forces Azure
// to re-apply it; a no-op redeploy does not re-run the script.
resource setupScript 'Microsoft.Compute/virtualMachines/extensions@2023-09-01' = {
  parent: vm
  name: 'jumpbox-setup'
  location: location
  dependsOn: [
    aadSshLogin
  ]
  properties: {
    publisher: 'Microsoft.Azure.Extensions'
    type: 'CustomScript'
    typeHandlerVersion: '2.1'
    autoUpgradeMinorVersion: true
    settings: {
      fileUris: [
        setupScriptRawUrl
      ]
    }
    protectedSettings: {
      commandToExecute: 'bash jumpbox-setup.sh \'${adminUsername}\' \'${queueViewerRawUrl}\''
    }
  }
}

@description('Resource ID of the VM.')
output id string = vm.id

@description('Name of the VM.')
output name string = vm.name

@description('System-assigned managed identity principal ID -- grant this Service Bus data-plane roles.')
output principalId string = vm.identity.principalId
